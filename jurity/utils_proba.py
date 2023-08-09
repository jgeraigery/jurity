import numpy as np
import warnings
import pandas as pd
import scipy.stats
from sklearn.linear_model import LinearRegression
from jurity.utils import Union, List, InputShapeError, WeightTooLarge, Constants, check_inputs_proba


def get_bootstrap_results(predictions: Union[List, np.ndarray, pd.Series],
                          memberships: Union[List, np.ndarray, pd.Series, List[List], pd.DataFrame],
                          surrogates: Union[List, np.ndarray, pd.Series],
                          membership_labels: Union[str, int, List[str], List[int]],
                          labels: Union[List, np.ndarray, pd.Series] = None,
                          bootstrap_trials: int = Constants.bootstrap_trials,
                          membership_names: List[str] = None) -> pd.DataFrame:
    # TODO add pydoc, add return type hint on what's returned from this call

    # Z is the number of unique values for the surrogate variable.
    # p is the length of the interior vectors of memberships

    # Summarize input data by surrogate variable to create the following:
    # Y [Z by 4]: A summary of the confusion matrix, with percentage of individuals in each quadrant
    #   Using predictions and labels to calculate the confusions matrix quadrant for each individual
    #   summarize a surrogate level as true_positive_ratio, false_positive_ration, true_negative_ratio, false_negative_ratio
    #   this lets us calculate inferred values for protected and unprotected groups
    #   Note: If labels are not available, Y will be [Z by 1] with only the percent_positive_ratio
    # X [Z by p]: Likelihoods at surrogate variable level, p>=2 with the probabilities of protected and unprotected status
    # W [Z by 1]: The number of individuals for each level of Z
    # Right now, these are returned as a single dataframe
    if membership_names is None:
        if isinstance(memberships,pd.DataFrame):
            membership_names=memberships.columns
        else:
            membership_names=["A","B"]
    if labels is not None:
        check_inputs_proba(predictions, memberships, surrogates, membership_labels, membership_names,True, labels)
    else:
        check_inputs_proba(predictions, memberships, surrogates, membership_labels, membership_names,False)
    summary_df = SummaryData.summarize(predictions, memberships, surrogates, labels, membership_names)

    # Add X, Y, and W matricies to the BiasCalculator
    if labels is not None:
        bc = BiasCalculator.from_df(summary_df, membership_labels, membership_names)
    else:
        bc = BiasCalculator.from_df(summary_df, membership_labels, membership_names, test_names=[Constants.prediction_ratio])

    # Run bootstrapping to calculate inferred metrics
    # for 1 to 100:
    #   sample Z rows from X, Y, and W with replacement
    #   Calculate the WOLS estimates for:
    #       true_positive_ratio, false_positive_ration, true_negative_ratio, false_negative_ratio for protected and unprotected groups
    #       Calculate TPR, FNR, FPR, and TNR for the sample.
    #       Store the estimates.
    bootstrap_df = bc.run_bootstrap(bootstrap_trials)

    # Calculate the means of all statistics across the 100 bootstrapped samples
    # Calculate average odds, equal opportunity, predictive equality, false negative difference, statistical parity
    transformed_bootstrap = bc.transform_bootstrap_results(bootstrap_df)
    return transformed_bootstrap


def unpack_bootstrap(df: pd.DataFrame, stat_name: str, membership_labels: List[int]):
    stats = df[[stat_name]]
    v = stats.index.values
    if len(v) != 2 or len(membership_labels)>1:
        raise ValueError("Unpacking for probabilistic results only enabled for binary metrics.")
    return stats.loc[v[membership_labels], stat_name][0], stats.loc[np.delete(v, membership_labels), stat_name][0]


class BiasCalculator:
    """
    Members:
    _y: measurements to be calculated
    _x: Input race percentages
    _w: Weights for weighted regression
    _surrogate_labels: Labels for race columns
    _test_labels: Labels for test columns
    """

    @classmethod
    def from_df(cls,df, membership_labels, membership_names, test_names=None, weight_name="count",weight_warnings=True):
        if test_names is None:
            test_names = [Constants.true_positive_ratio, Constants.true_negative_ratio,
                          Constants.false_positive_ratio, Constants.false_negative_ratio,
                          Constants.prediction_ratio]

        if np.any([m < 0 for m in membership_labels]) or np.any(
                [m >= len(membership_names) for m in membership_labels]):
            raise ValueError(
                f"Protected membership_label:{membership_labels} not in membership_names:{membership_names}.")

        bcdf = BiasCalcFromDataFrame(membership_names, weight_name, membership_labels, test_names)

        # Start with min_weight set at the recommended default.
        # If it's too high, set at 10.
        # If 10 is too high, set at 1 and give a warning.
        try:
            bc = bcdf.get_bias_calculator(df)
        except WeightTooLarge:
            try:
                bc = bcdf.get_bias_calculator(df, 10)
            except WeightTooLarge:
                bc = bcdf.get_bias_calculator(df, 1, weight_warnings=weight_warnings)
        return bc

    def __init__(self, Y, X, W, surrogate_labels, test_labels, verbose=True):
        self.Y(Y)
        self.X(X)
        self.W(W)
        if surrogate_labels:
            self.surrogate_labels(surrogate_labels)
        if test_labels:
            self.test_labels(test_labels)
        self.check_dimensions()
        self.verbose(verbose)

    def verbose(self, value=None):
        if value is not None:
            self._verbose = value
        return self._verbose

    def Y(self, value=None):
        """
        Get or set input y as a numpy array that has 2 dimensions.
        Arguments:
            value: Value to be set
        """
        if value is not None:
            try:
                y = np.array(value, dtype='f')
            except ValueError:
                print("Cannot convert Y input to a numpy array of floats.")
                return None
            if y.ndim == 1:
                self._y = np.reshape(y, (-1, y.shape[0]))
            elif not y.ndim == 2:
                raise ValueError("Input Y must have 1 or 2 dimensions.")
            self._y = y
            # always update n_tests when we update Y
            self._n_tests = self._y.shape[1]
        return self._y

    def X(self, value=None):
        """
        Get or set input x as a  numpy array that has 2 dimensions.
        """
        if value is not None:
            try:
                x = np.array(value, dtype='f')
            except ValueError:
                print("Cannot convert X into a numpy array of floats.")
            if x.ndim == 1:
                self._x = np.reshape(x, (-1, x.shape[0]))
            elif not x.ndim == 2:
                raise ValueError("Input X must have 1 or 2 dimensions")
            self._x = x
        return self._x

    def W(self, value=None):
        """
        Get or set input w as a numpy array that has 1 dimension
        """
        if value is not None:
            try:
                w = np.array(value, dtype='f')
            except ValueError:
                print("Cannot convert W into a numpy array or floats.")
            if not w.ndim == 1:
                raise ValueError("W must be a one-dimensional array.")
            self._w = w
        return self._w

    def surrogate_labels(self, value=None):
        """
        Set and get labels for race statistics. Input value must be a 2D list
        """
        r_form = "Surrogate labels list must be of form [[omitted category],[race_1, race_2,...]]"
        input_form = f"Input was of form {value}"
        if value:
            if not isinstance(value, list):
                raise ValueError(f"{r_form}\n{input_form}")
            elif not len(value) == 2:
                raise ValueError(f"{r_form}\n{input_form}")
            elif not len(value[0]) == 1:
                raise ValueError("There can only be one omitted category")
            else:
                if len(value[1]) != self._x.shape[1]:
                    raise ValueError(
                        "Surrogate labels does not match dimension of X. Update X first, or check dimensions of input labels.")
                else:
                    self._compare_label = value[0][0]
                    self.all_surrogate_labels(value[1])
        return [[self._compare_label], self.all_surrogate_labels()]

    def all_surrogate_labels(self, value=None):
        if value is not None:
            # TODO: sanitize
            self._all_surrogate_labels = value
        return self._all_surrogate_labels

    def test_labels(self, value=None):
        if value is not None:
            if not isinstance(value,list):
                raise ValueError(f"test_labels must be a list of strings. Input: {value}")
            else:
                for s in value:
                    if not isinstance(s,str):
                        raise ValueError(f"test_labels must be a list of strings. Input: {value}")
            self._test_labels = value
        return self._test_labels

    def check_dimensions(self):
        """
        When change are made, check the dimensions of X, Y, and W to make sure they still match.
        """
        if not self.Y().shape[0] == self.X().shape[0]:
            raise ValueError(
                "Dimensions of X and Y do not match. Dimensions of X:- {0}, Dimensions of Y: {1}".format(self._x.shape,
                                                                                                         self._y.shape))
        elif not self.Y().shape[0] == self.W().shape[0]:
            raise ValueError("Length of W does not match X and Y")
        elif not self.Y().shape[1]==len(self.test_labels()):
            raise ValueError("Y and test_names have different dimensions. Y:{0} labels: {1}.".format(self.Y().shape[1],len(self.test_labels())))
        else:
            return True

    def calc_one_bag(self, in_X, in_Y, in_W):
        """
        Calculate the regression of all surrogate-based tests
        in_X: The sampled X for this bootstrap
        in_Y: The sampled Y for this bootstrap
        in_W: The sampled weights for this bootstrap
        """
        all_models = {}
        names_of_Ys = self.test_labels()
        # Run one regression
        for i in range(len(names_of_Ys)):
            y = in_Y[:, i]
            all_models[names_of_Ys[i]] = LinearRegression().fit(in_X, y, in_W)
        if self.verbose():
            for k in list(all_models.keys()):
                # TODO this m is not used, so sth is off with the for-loop?
                m = all_models[k]
        return all_models

    def run_bootstrap(self, bootstrap_trials):
        """
        Collect data for final calculation using the bootstrap
        Returns a dataframe with a column for each calculation and a row for bootstrap sample
        Inputs: n_boots: Number of bootstrapped samples
        """
        all_model_results = []
        n_rows = self.X().shape[0]
        for i in range(bootstrap_trials):
            select_these = np.random.choice(range(0, n_rows), n_rows)
            in_X = self.X()[select_these]
            in_Y = self.Y()[select_these]
            in_W = self.W()[select_these]
            models = self.calc_one_bag(in_X, in_Y, in_W)
            for k in list(models.keys()):
                m = models[k]
                model_result_dict = {"run_id": [i], "stat_name": [k], self.surrogate_labels()[0][0]: m.intercept_}
                # This depends on sklearn returning a coefficient array that is in the same
                # order as the input X's. This is a reasonable assumption--scoring doesn't work without it.
                n_xs = in_X.shape[1]
                coefs_with_names = {self.surrogate_labels()[1][j]: m.coef_[j] for j in range(n_xs)}
                model_result_dict.update(coefs_with_names)
                df = pd.DataFrame(model_result_dict)
                all_model_results.append(df)
        return pd.concat(all_model_results)

    def transform_bootstrap_results(self, df):
        """
        Calculate TNR, TPR, FNR, FPR, and Accuracy
        Input: Pandas Dataframe that is the result of self.run_bootstrap
        Returns: Transposed version of input dataframe with added columns (if applicable)
        """
        means = df.groupby("stat_name").mean()
        del means["run_id"]

        results_by_race = means.T
        tests_we_have = results_by_race.columns
        # For binary classifiers, if we know the true labels, we will probably want these tests.
        common_tests = ["false_positive_ratio", "false_negative_ratio", "true_positive_ratio", "true_negative_ratio"]
        if set(common_tests).issubset(set(tests_we_have)):
            results_by_race["FPR"] = results_by_race["false_positive_ratio"] / (
                    results_by_race["false_positive_ratio"] + results_by_race["true_negative_ratio"])
            results_by_race["FNR"] = results_by_race["false_negative_ratio"] / (
                    results_by_race["false_negative_ratio"] + results_by_race["true_positive_ratio"])
            results_by_race["TPR"] = results_by_race["true_positive_ratio"] / (
                    results_by_race["true_positive_ratio"] + results_by_race["false_negative_ratio"])
            results_by_race["TNR"] = results_by_race["true_negative_ratio"] / (
                    results_by_race["true_negative_ratio"] + results_by_race["false_positive_ratio"])
            results_by_race["ACC"] = results_by_race["true_positive_ratio"] + results_by_race[
                "true_negative_ratio"]
        # For binary classifiers we can always calculate the prediction_ratio, even if we don't have anything else
        if "prediction_ratio" in tests_we_have:
            results_by_race["Prediction Rate"] = results_by_race["prediction_ratio"]
        return results_by_race

    # TODO cannot we use sth from utils and/or merge with something in utils
    # TODO I feel we are duplicating many such calculations (I can be wrong)
    @staticmethod
    def calc_rates(tp_ratio, fp_ratio, tn_ratio, fn_ratio):
        """
        Calculates false positive rate, false negative rate, ... rates given their ratios
        """
        return tp_ratio / (tp_ratio + fn_ratio), fp_ratio / (fp_ratio + tn_ratio), tn_ratio / (
                tn_ratio + fp_ratio), fn_ratio / (fn_ratio + tp_ratio)

    def calc_fairness_metrics(self, boot):
        """
        Takes untransformed bootstrap results and calculates fairness metrics
        Bootstrap results are in the form of percentages; not counts
        boot: untransformed bootstrap results
        """
        for name, group in boot.groupby("run_id"):
            tp_ratio = group[group["stat_name"] == "true_positive_ratio"].iloc[0].to_numpy()[2:]
            fp_ratio = group[group["stat_name"] == "false_positive_ratio"].iloc[0].to_numpy()[2:]
            tn_ratio = group[group["stat_name"] == "true_negative_ratio"].iloc[0].to_numpy()[2:]
            fn_ratio = group[group["stat_name"] == "false_negative_ratio"].iloc[0].to_numpy()[2:]

            tp_ratio += tp_ratio[0]
            tp_ratio[0] /= 2
            fp_ratio += fp_ratio[0]
            fp_ratio[0] /= 2
            tn_ratio += tn_ratio[0]
            tn_ratio[0] /= 2
            fn_ratio += fn_ratio[0]
            fn_ratio[0] /= 2
            tpr, fpr, tnr, fnr = self.calc_rates(tp_ratio, fp_ratio, tn_ratio, fn_ratio)

            stats = {}
            stats["avg_odds"] = 0.5 * (fpr - fpr[0] + tpr - tpr[0])
            stats["disparate_impact"] = (tpr + fnr) / (tpr[0] + fnr[0])
            stats["equal_opportunity"] = tpr - tpr[0]
            stats["fnr_test"] = fnr - fnr[0]
            stats["predictive_equality"] = fpr - fpr[0]
            stats["statistical_parity"] = tpr + fnr - tpr[0] - fnr[0]
            boot = pd.concat(
                [boot] + [pd.DataFrame([[name, k] + list(v)], columns=boot.columns) for (k, v) in stats.items()])
        return boot

    def transform_boot_linear_models(self, boot, cf=0.99):
        """
        Calculate 99% confidence limit for difference between mean residual for white and other races
        Arguments
        boot: Results from bootstrapped data
        """
        n = boot.shape[0]
        means = boot.groupby("stat_name").mean()
        std = boot.groupby("stat_name").std()
        h = std * scipy.stats.t.ppf((1 + cf) / 2., n - 1)
        lower_ci = means - h
        upper_ci = means + h
        del means["run_id"], std["run_id"], lower_ci["run_id"], upper_ci["run_id"]
        comp_group = self._compare_label
        del means[comp_group], std[comp_group], lower_ci[comp_group], upper_ci[comp_group]

        pass_test = ((lower_ci < 0) & (upper_ci > 0))
        # TODO no_diff is not used? Remove?
        no_difference = (lower_ci > 0) & (upper_ci < 0)
        means.columns = means.columns + "_means"
        std.columns = std.columns + "_std"
        upper_ci.columns = upper_ci.columns + "_upper_ci"
        lower_ci.columns = lower_ci.columns + "_lower_ci"
        return pd.concat([means, lower_ci, upper_ci], axis=1).T.sort_index(), pass_test

    def __str__(self):
        return "BiasCalculator(surrogate_labels=" + str(self.surrogate_labels()) + ", test_labels=" + str(
            self.test_labels()) + ")"


class BiasCalcFromDataFrame:
    """
    Class that creates a bias calculator from a dataframe.
    Class Variables:
    _surrogate_names: Names of race percentages
    _weight_name: Name of column with weights
    _compare_label: label of group that's comparison group from the regression
    _test_names: Names of tests to be calculated
    """

    def __init__(self, membership_names, weight_name, membership_labels, test_names):
        """
        Initialize names to be read and name of comparison category for regression.
        """

        omitted_string=None
        # Get the first non-protected group listed.
        for idx, name in enumerate(membership_names):
            if idx not in membership_labels:
                omitted_string = name
                break
        if omitted_string is None:
            raise ValueError("All groups appear to be protected groups. Must designate at least one non-protected group.")
        self.compare_label(omitted_string)
        self.surrogate_names(membership_names)
        if self._compare_label in membership_names:
            self._surrogate_names.remove(self._compare_label)
        self.test_names(test_names)
        self.weight_name(weight_name)

    def surrogate_names(self, value=None):
        """
        Get or set surrogate class names. Make sure it is a list of strings
        """
        if value:
            if type(value) != list:
                raise ValueError("Surrogate class names must be a list of strings")
            v = list(set(value))
            for l in v:
                if not isinstance(l, str):
                    raise ValueError(f"Surrogate class name {l} is not a string.")
            self._surrogate_names = value
            if not len(self._surrogate_names) == len(v):
                raise ValueError("Surrogate class name contains duplicates.")
        return self._surrogate_names

    def test_names(self, value=None):
        """
        Get or set names of test columns. Make sure it is a list of strings
        """
        if value:
            if type(value) != list:
                raise ValueError("Test names must be a list of strings")
            v = list(set(value))
            for l in v:
                if not isinstance(l, str):
                    raise ValueError(f"Test name {l} is not a string.")
            self._test_names = value
            if not len(self._test_names) == len(v):
                raise(ValueError,"List of test names contains duplicates.")
        return self._test_names

    def compare_label(self, value=None):
        """
        Get or set compare group. Make sure it is a single string.
        """
        if value:
            e = "Name of omitted category is {0}, not a string."
            if self.check_single_string(value, e):
                self._compare_label = value
        return self._compare_label

    def pred_name(self, value=None):
        """
        Get or set name of column with predictions. Make sure it is a single string.
        """
        if value:
            e = "Name of predicted column is {0}, not a string."
            if self.check_single_string(value, e):
                self._pred_name = value
        return self._pred_name

    def true_name(self, value=None):
        """
        Get or set name of column with true classifications. Make sure it is a single string.
        """
        if value:
            e = "Name of predicted column is {0}, not a string."
            if self.check_single_string(value, e):
                self._true_name = value
        return self._true_name

    def weight_name(self, value=None):
        """
        Get or set name of weight column. Make sure it is a single string.
        """
        if value:
            e = "Name of weight column is {0}, not a string."
            if self.check_single_string(value, e):
                self._weight_name = value
        return self._weight_name

    @staticmethod
    def check_single_string(value, error_msg):
        """
        Helper function to check inputs that are supposed to be single strings
        """
        if not isinstance(value, str):
            raise ValueError(error_msg.format(value))
        else:
            return True

    def get_X_matrix(self, df):
        """
        Make X matrix for bias calculator.
        """
        if not set(self.surrogate_names()).issubset(set(df.columns)):
            raise ValueError(
                "Surrogate names: {0} are not in dataframe.".format(set(self._surrogate_names) - (set(df.columns))))
        return df[self.surrogate_names()].to_numpy(dtype='f')

    def get_Y_matrix(self, df):
        """
        Make Y matrix for bias calculator.
        """
        if not set(self._test_names).issubset(set(df.columns)):
            raise ValueError("Test names: {0} are not in dataframe.".format(set(self._test_names) - (set(df.columns))))
        return df[self._test_names].to_numpy(dtype='f')

    def get_W_array(self, df):
        """
        Make W array for bias calculator.
        """
        if not self._weight_name in set(df.columns):
            raise ValueError("weight name: {0} are not in dataframe.".format(self._weight_name))
        return df[self._weight_name].to_numpy(dtype='f')

    def get_bias_calculator(self, df, min_weight=30, weight_warnings=True):
        """
        Make bias calculator.
        """
        if min_weight < 10:
            warnings.warn("Recommended minimum count for surrogate class is 30. "
                          "Minimum weights of less than 10 will give unstable results.")

        if self.weight_name() in df.columns:
            subset = df[df[self._weight_name] >= min_weight]
            print("{0} rows removed from datafame for insufficient weight values" \
                  .format(df.shape[0] - subset.shape[0]))
            if subset.shape[0] < len(self.surrogate_names()):
                raise WeightTooLarge("Input dataframe does not have enough rows to estimate surrogate classes "
                                     "reduce minimum weight.")
        else:
            raise ValueError("Weight variable {0} is not in dataframe.".format(self.weight_name()))

        # Create Bias Calculator
        X = self.get_X_matrix(subset)
        Y = self.get_Y_matrix(subset)
        W = self.get_W_array(subset)
        bc = BiasCalculator(Y, X, W, [[self.compare_label()], self.surrogate_names()], self.test_names())

        return bc

    def __str__(self):
        return "BiasCalculatorFromDataFrame(surrogate_names=" + str(self.surrogate_names()) + ", test_names=" + str(
            self.test_names()) + ")"


class SummaryData:
    """
    Class that calculates summary data by surrogate class from input detailed data by surrogate class.
    _tests: Names of tests to be calculated
    _surrogate_perf_col_name: Name of zip variable in performance data
    _surrogate_surrogate_col_name: Name of zip variable in zip code data
    _pred_name: Name of column with predicted label
    _true_name: Name of column with true label
    """

    @classmethod
    def summarize(cls,
                  predictions: Union[List, np.ndarray, pd.Series],
                  memberships: Union[List, np.ndarray, pd.Series, pd.DataFrame],
                  surrogates: Union[List, np.ndarray, pd.Series],
                  labels: Union[List, np.ndarray, pd.Series] = None,
                  membership_names: List[str] = None) -> pd.DataFrame:
        """
        Return a summary dataframe suitable for bootstrap calculations.
        """
        if membership_names is None:
            membership_names = ["A", "B"]

        df = pd.concat([pd.Series(predictions, name="predictions"),
                        pd.Series(surrogates, name="surrogates")], axis=1)

        if labels is not None:
            df = pd.concat([df, pd.Series(data=labels, name="labels")], axis=1)
            label_name = "labels"
            test_names = [Constants.true_positive_ratio, Constants.true_negative_ratio,
                          Constants.false_positive_ratio, Constants.false_negative_ratio,
                          Constants.prediction_ratio]
        else:
            label_name = None
            test_names = [Constants.prediction_ratio]
        # To specify likelihoods, user can provide either:
        # 1. An ndarray of likelihoods that gives likelihood of protected membership
        #   for each person. If this is given, we have to summarize the likelihoods at the surrogate level
        # 2. A dataframe ttehat has a row for each surrogate class value and
        #   a column for each likelihood value. The dataframe must have surrogate class as an index.
        if isinstance(memberships, pd.DataFrame):
            memberships["surrogates"] = memberships.index
            likes_df = memberships
        else:
            if len(memberships) != df.shape[0]:
                len_predictions = len(predictions)
                len_likelihoods = len(memberships)
                raise InputShapeError("",
                                      "Likelihoods must either be a pandas dataframe with surrogates as index "
                                      "or be the same length as predictions vector"
                                      f"length of predictions {len_predictions}"
                                      f"length of likelihoods {len_likelihoods}")
            if isinstance(memberships, list) or isinstance(memberships, np.ndarray):
                interim_df = pd.DataFrame(data=memberships)
            elif isinstance(memberships, pd.Series):
                interim_df = pd.DataFrame(list(memberships.values))
            else:
                interim_df = memberships
            likes_detail = pd.concat([pd.Series(surrogates, name="surrogates"), interim_df], axis=1)
            likes_df = likes_detail.groupby(by="surrogates").mean()
            likes_df.columns = membership_names
            likes_df = likes_df.reset_index()
        summarizer = cls("surrogates", "surrogates", "predictions", true_name=label_name, test_names=test_names)
        return summarizer.make_summary_data(perf_df=df, surrogate_df=likes_df)

    def __init__(self, surrogate_surrogate_col_name, surrogate_perf_col_name, pred_name, true_name=None, max_shrinkage=0.5,
                 test_names=None):
        self.surrogate_surrogate_col_name(surrogate_surrogate_col_name)
        self.surrogate_perf_col_name(surrogate_perf_col_name)
        self.pred_name(pred_name)
        self._true_name = None
        self.true_name(true_name)
        self.max_shrinkage(max_shrinkage)
        self._test_names = None
        self.test_names(test_names)

    def surrogate_surrogate_col_name(self, value=None):
        if value:
            if self.col_name_checker(value):
                self._surrogate_surrogate_col_name = value
        return self._surrogate_surrogate_col_name

    def surrogate_perf_col_name(self, value=None):
        if value:
            if self.col_name_checker(value):
                self._surrogate_perf_col_name = value
        return self._surrogate_perf_col_name

    def pred_name(self, value=None):
        if value:
            if self.col_name_checker(value):
                self._pred_name = value
        return self._pred_name

    def true_name(self, value=None):
        if value:
            if self.col_name_checker(value):
                self._true_name = value
        return self._true_name

    def max_shrinkage(self, value=None):
        if value:
            if isinstance(value, float) and value < 1 and value > 0:
                self._max_shrinkage = value
            else:
                raise ValueError(f"Max shrinkage must be a float between 0 and 1. input value is: {value}.")
        return self._max_shrinkage

    def col_name_checker(self, value):
        if not isinstance(value, str):
            raise ValueError(f"Column names must be strings. {value} is not a string.")
            # return False
        return True

    def check_performance_data(self, df):
        """
        Checks specific to performance data.
        """
        if self.true_name() is not None:
            needed_names = [self.true_name()] + [self.pred_name()] + [self.surrogate_perf_col_name()]
        else:
            needed_names = [self.pred_name()] + [self.surrogate_perf_col_name()]
        return self.check_read_data(df, needed_names, "performance_data")

    def check_read_data(self, df, needed_names, df_name, id_col_name=None):
        """
        Sanity check input data and raise errors when there are issues.
        """
        n_rows = df.shape[0]
        all_good = True
        if id_col_name:
            n_unique_ids = df[id_col_name].nunique()
            if not n_rows == n_unique_ids:
                raise Warning(f"Number of unique ids in {df_name} is: {n_unique_ids} but number of rows is {n_rows}")
        print(f"There are {n_rows} in {df_name}.")
        names = df.columns
        if not set(needed_names).issubset(set(names)):
            raise ValueError("Some necessary columns not in {0} data: {1} are missing.".format(df_name, list(
                set(needed_names) - set(names))))
        return all_good

    def test_names(self, value=None):
        if value:
            if isinstance(value, list):
                self._test_names = value
            else:
                raise ValueError(f"Test names must be a list. {value} is not a list")
        if not self._test_names:
            if self.pred_name() and self.true_name():
                self._test_names = ["true_positive", "true_negative", "false_positive", "false_negative"]
            else:
                self._test_names = ["prediction_ratio"]
        return self._test_names

    def check_surrogate_data(self, df):
        """
        Checks specific to zip data.
        """
        all_good = True
        needed_names = [self._surrogate_surrogate_col_name]
        if not self.check_read_data(df, needed_names, "zip data"):
            all_good = False
            return all_good
        else:
            all_good = (df[self._surrogate_surrogate_col_name].nunique() == df.shape[0])
            if not all_good:
                print("Input Zip data has duplicates. Zip data must be de-duplicated by zip.")
        return all_good

    def check_merged_data(self, merged_df, surrogate_df, performance_df, print_warnings=True):
        """
        Make sure merged data hasn't lost too many rows due to inner join
        And make sure it hasn't increased in rows due to zip code duplicates
        Arguments:
        merged_df: data frame resulting from merge between surrogate_df and performance_df
        surrogate_df: zip code data frame
        performance_df: Performance data frame
        """

        m_rows = float(merged_df.shape[0])
        z_rows = float(surrogate_df.shape[0])
        p_rows = float(performance_df.shape[0])
        shrinkage = 1 - m_rows / p_rows

        if print_warnings:
            if shrinkage < 0:
                raise Warning(
                    f"Merged data has {m_rows}. Input performance data has {p_rows}. There may be duplicate zip codes in zip code data.")
            elif shrinkage > self.max_shrinkage():
                print(f"Merged data has {m_rows}, but performance data only has {p_rows}.")
                raise ValueError(
                    "Merge between surrogate data and performance data results in loss of {0:.0}% of performance data.".format(
                        shrinkage))
            elif shrinkage > 0.2:
                warnings.warn(f"Merged data has {m_rows}, but performance data has {p_rows}.")
                # raise Warning("Merge between zip code data and performance data rows results in loss of {0:.0}% of performance data.".format(shrinkage))

    def check_surrogate_confusion_matrix(self, confusion_df, merged_df):
        """
        Make sure confusion matrix is unique by zip code.
        Make sure nothing has been lost in the summary.
        Arguments:
            confusion_df: The summary by zip code
            merged_df: the original detail df.
        """
        n_rows = confusion_df.shape[0]
        n_unique_zips = merged_df[self._surrogate_surrogate_col_name].nunique()
        if not n_rows == n_unique_zips:
            # TODO This keeps printing during tests, can we turn off?
            print(
                f"Final dataframe has {n_rows} and {n_unique_zips} unique zip codes. There should be one row per zip code.")
            raise Warning("Possible missing zip codes in output data.")
            # return False
        return True

    def make_summary_data(self, perf_df, surrogate_df=None):
        """
        Function that merges two dfs to make a surrogate-based summary file.
        And has the needed accuracy.
        Arguments:
        surrogate_df: a dataframe unique by zip code that has race percentages for the zip code.
        perf_df: a dataframe that has zip code and performance columns
        """
        self.check_performance_data(perf_df)
        self.check_surrogate_data(surrogate_df)
        merged_data = perf_df.merge(surrogate_df, left_on=self.surrogate_perf_col_name(), right_on=self.surrogate_surrogate_col_name())
        self.check_merged_data(merged_data, surrogate_df, perf_df)

        # Create accuracy columns that measure true positive, true negative etc
        accuracy_df = pd.concat([merged_data[self.surrogate_surrogate_col_name()],
                                 self.confusion_matrix_actual(merged_data, self.pred_name(), self.true_name())], axis=1)
        # Use calc_accuracy_metrics to create surrogate-level summary
        confusion_matrix_surrogate_summary = self.calc_accuracy_metrics(accuracy_df)
        self.check_surrogate_confusion_matrix(confusion_matrix_surrogate_summary, merged_data)
        return confusion_matrix_surrogate_summary.join(surrogate_df.set_index(surrogate_df[self.surrogate_surrogate_col_name()]))

    # Add columns to a pandas dataframe flagging each row as false positive, etc.
    def confusion_matrix_actual(self, test_df, pred_col, label_col):
        """
        Construct 0/1 variables for membership in a quadrant of the confusion matrix
        Arguments: test_df: dataframe with detail data that has a pred_column and a label_column
        pred_col: 0/1 predicted in class or not.
        label_col: 0/1 true label
        """
        #TODO: Replace this with confusion_matrix from utils.py
        if label_col is not None:
            correct = (test_df[pred_col] == test_df[label_col]).astype(int)
            correct.name = "correct"
            true_positive = (correct & (test_df[label_col] == 1)).astype(int)
            true_positive.name = "true_positive"
            true_negative = (correct & (test_df[label_col] == 0)).astype(int)
            true_negative.name = "true_negative"
            false_negative = ((correct == False) & (test_df[pred_col] == 0)).astype(int)
            false_negative.name = "false_negative"
            false_positive = ((correct == False) & (test_df[pred_col] == 1)).astype(int)
            false_positive.name = "false_positive"
            return pd.concat([true_positive, true_negative, false_positive, false_negative, test_df[pred_col], correct],
                             axis=1)
        else:
            # This means we only have predictions and no true labels
            return test_df[pred_col]

    def calc_accuracy_metrics(self, test_df):
        """
        Calculate TPR, etc from the confusion matrix columns
        Arguments:
            test_df: dataframe with detail data that will be rolled up by zip code
            group_col: surrogate column name
            acc_cols: accuracy columns that are in the dataframe as 0/1 and will be rolled up by zip
        """
        group_col=[self._surrogate_perf_col_name]

        if self.true_name() is not None:
            acc_cols = ["true_positive","true_negative",
                        "false_positive","false_negative"]
        else:
            acc_cols=[]

        agg_dict = {}
        ac_cols = acc_cols + [self.pred_name()]
        for c in ac_cols:
            agg_dict[c] = "sum"
        agg_dict[group_col[0]] = "count"
        check_accuracy = test_df \
            .groupby(group_col) \
            .agg(agg_dict) \
            .rename(columns={group_col[0]: "count"})
        for c in ac_cols:
            check_accuracy[c + "_ratio"] = check_accuracy[c] / check_accuracy["count"]
        check_accuracy = check_accuracy.rename(columns={"_".join([self.pred_name(), "ratio"]): Constants.prediction_ratio})

        out_cols = ["prediction_ratio", "count"]

        if {Constants.true_negative_ratio, Constants.true_positive_ratio, Constants.false_negative_ratio, Constants.false_positive_ratio}.issubset(
                set(check_accuracy.columns)):
            out_cols = out_cols + [Constants.true_negative_ratio, Constants.true_positive_ratio,
                                   Constants.false_negative_ratio, Constants.false_positive_ratio]
            # Return a dataframe that has the stats by group. Use these to compare to expected values
        return check_accuracy[out_cols]