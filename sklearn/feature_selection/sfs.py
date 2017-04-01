"""
Sequential feature selection

"""

# Author: Sebastian Raschka <se.raschka@gmail.com>
#
# License: BSD 3 clause

import numpy as np
from itertools import combinations
from collections import defaultdict
from ..base import BaseEstimator
from ..base import MetaEstimatorMixin
from ..base import clone
from ..utils.validation import check_is_fitted
from ..externals import six
from ..model_selection import cross_val_score
from ..metrics import get_scorer


class SFS(BaseEstimator, MetaEstimatorMixin):
    """Feature selector that selects features via greedy search.

    This Sequential Feature Selector (SFS) adds (forward selection) or
    removes (backward selection) the features (X) to form a feature subset
    in a greedy fashion that optimizes the extrinsic performance metric
    of a Regressor or Classifier on the desired ouputs (y).

    Read more in the :ref:`User Guide <sequential_feature_selection>`.

    Parameters
    ----------
    estimator : scikit-learn Classifier or Regressor

    k_features : int or tuple (default=1)
        An integer arguments specifies the number of features to select,
        where k_features < the full feature set.
        Optionally, a tuple containing a min and max value can be provided
        so that the feature selector will return a feature subset between
        with min <= n_features <= max that scored highest in the evaluation.
        For example, the tuple (1, 4) will return any combination from
        1 up to 4 features instead of a fixed number of features k.

    scoring : str, callable, or None (default=None)
        A string (see model evaluation documentation) or a scorer
        callable object / function with signature `scorer(estimator, X, y)`.

    forward : bool (default=True)
        Performs forward selection if True and backward selection, otherwise.

    cv : int or cross-validation generator, or an iterable (default=5)
        Determines the cross-validation splitting strategy for
        feature selection. Possible inputs for cv are:
        - 0 or None, don't use cross validation
        - integer > 1, to specify the number of folds in a (Stratified)KFold
        - An object to be used as a cross-validation generator.
        - An iterable yielding train, test splits.
        For integer/None inputs, if the estimator is a classifier
        and `y` is either binary or multiclass, `StratifiedKFold` is used.
        In all other cases, `KFold` is used.

    n_jobs : int (default=1)
        The number of CPUs to use for cross validation.

    pre_dispatch : int, or string (default: '2*n_jobs')
        Controls the number of jobs that get dispatched
        during parallel execution in cross_val_score.
        Reducing this number can be useful to avoid an explosion of
        memory consumption when more jobs get dispatched than CPUs can process.
        This parameter can be:
        - None, in which case all the jobs are immediately created and spawned.
          Use this for lightweight and fast-running jobs,
          to avoid delays due to on-demand spawning of the jobs
        - An int, giving the exact number of total jobs that are spawned
        - A string, giving an expression as a function
            of `n_jobs`, as in `2*n_jobs`

    Attributes
    ----------
    k_feature_idx_ : array-like, shape = [n_predictions]
        Feature Indices of the selected feature subsets.

    k_score_ : float
        Cross validation average score of the selected subset.

    subsets_ : dict
        A dictionary of selected feature subsets during the
        sequential selection, where the dictionary keys are
        the lengths k of these feature subsets. The dictionary
        values are dictionaries themselves with the following
        keys: 'feature_idx' (tuple of indices of the feature subset)
              'cv_scores' (list individual cross-validation scores)
              'avg_score' (average cross-validation score)

    Examples
    --------
    The following example shows how to use the sequential feature selector
    with default settings to select a feature subset, consisting of
    1 to 3 features, from iris. The selection criteria for this
    feature subset is the average cross-validation performance
    (cv=5 by default) of the `estimator` (here: KNN)
    during the greedy forward selection search.

        >>> from sklearn.feature_selection import SFS
        >>> from sklearn.neighbors import KNeighborsClassifier
        >>> from sklearn.datasets import load_iris
        >>> iris = load_iris()
        >>> X, y = iris.data, iris.target
        >>> knn = KNeighborsClassifier(n_neighbors=3)
        >>> sfs = SFS(knn, k_features=(1, 3))
        >>> sfs = sfs.fit(X, y)
        >>> round(sfs.k_score_, 4)
        0.9733
        >>> sfs.k_feature_idx_
        (0, 2, 3)
        >>> sfs.transform(X).shape
        >>> (150, 3)

    """
    def __init__(self, estimator, k_features=1,
                 forward=True, scoring=None,
                 cv=5, n_jobs=1,
                 pre_dispatch='2*n_jobs'):

        self.estimator = clone(estimator)
        self.k_features = k_features
        self.forward = forward
        self.pre_dispatch = pre_dispatch
        self.scoring = scoring
        if scoring is None:
            if self.estimator._estimator_type == 'classifier':
                scoring = 'accuracy'
            elif self.estimator._estimator_type == 'regressor':
                scoring = 'r2'
            else:
                raise ValueError('Estimator must '
                                 'be a Classifier or Regressor.')

        if isinstance(scoring, str):
            self.scorer = get_scorer(scoring)
        else:
            self.scorer = scoring
        self.cv = cv
        self.n_jobs = n_jobs
        self.named_est = {key: value for key, value in
                          _name_estimators([self.estimator])}

        self.subsets_ = {}

    def fit(self, X, y):
        """Perform feature selection and learn model from training data.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.
        y : array-like, shape = [n_samples]
            Target values.

        Returns
        -------
        self : object

        """
        if not isinstance(self.k_features, int) and\
                not isinstance(self.k_features, tuple):
            raise ValueError('k_features must be a positive integer'
                             ' or tuple')

        if isinstance(self.k_features, int) and (self.k_features < 1 or
                                                 self.k_features > X.shape[1]):
            raise ValueError('k_features must be a positive integer'
                             ' between 1 and X.shape[1], got %s'
                             % (self.k_features, ))

        if isinstance(self.k_features, tuple):
            if len(self.k_features) != 2:
                raise ValueError('k_features tuple must consist of 2'
                                 ' elements a min and a max value.')

            if self.k_features[0] not in range(1, X.shape[1] + 1):
                raise ValueError('k_features tuple min value must be in'
                                 ' range(1, X.shape[1]+1).')

            if self.k_features[1] not in range(1, X.shape[1] + 1):
                raise ValueError('k_features tuple max value must be in'
                                 ' range(1, X.shape[1]+1).')

            if self.k_features[0] > self.k_features[1]:
                raise ValueError('The min k_features value must be larger'
                                 ' than the max k_features value.')

        if isinstance(self.k_features, tuple):
            select_in_range = True
        else:
            select_in_range = False
            k_to_select = self.k_features

        self.subsets_ = {}
        orig_set = set(range(X.shape[1]))
        if self.forward:
            if select_in_range:
                k_to_select = self.k_features[1]
            k_idx = ()
            k = 0
        else:
            if select_in_range:
                k_to_select = self.k_features[0]
            k_idx = tuple(range(X.shape[1]))
            k = len(k_idx)
            k_score = self._calc_score(X, y, k_idx)
            self.subsets_[k] = {
                'feature_idx': k_idx,
                'cv_scores': k_score,
                'avg_score': k_score.mean()
                }

        best_subset = None
        k_score = 0

        while k != k_to_select:
            prev_subset = set(k_idx)
            if self.forward:
                k_idx, k_score, cv_scores = self._inclusion(
                    orig_set=orig_set,
                    subset=prev_subset,
                    X=X,
                    y=y
                )
            else:
                k_idx, k_score, cv_scores = self._exclusion(
                    feature_set=prev_subset,
                    X=X,
                    y=y
                )

            k = len(k_idx)
            if k not in self.subsets_ or (self.subsets_[k]['avg_score'] <
                                          k_score):
                self.subsets_[k] = {
                    'feature_idx': k_idx,
                    'cv_scores': cv_scores,
                    'avg_score': k_score
                }

        if select_in_range:
            max_score = float('-inf')
            for k in self.subsets_:
                if self.subsets_[k]['avg_score'] > max_score:
                    max_score = self.subsets_[k]['avg_score']
                    best_subset = k
            k_score = max_score
            k_idx = self.subsets_[best_subset]['feature_idx']

        self.k_feature_idx_ = k_idx
        self.k_score_ = k_score
        return self

    def _calc_score(self, X, y, indices):
        if self.cv:
            scores = cross_val_score(self.estimator,
                                     X[:, indices], y,
                                     cv=self.cv,
                                     scoring=self.scorer,
                                     n_jobs=self.n_jobs,
                                     pre_dispatch=self.pre_dispatch)
        else:
            self.estimator.fit(X[:, indices], y)
            scores = np.array([self.scorer(self.estimator, X[:, indices], y)])
        return scores

    def _inclusion(self, orig_set, subset, X, y):
        all_avg_scores = []
        all_cv_scores = []
        all_subsets = []
        res = (None, None, None)
        remaining = orig_set - subset
        if remaining:
            for feature in remaining:
                new_subset = tuple(subset | {feature})
                cv_scores = self._calc_score(X, y, new_subset)
                all_avg_scores.append(cv_scores.mean())
                all_cv_scores.append(cv_scores)
                all_subsets.append(new_subset)
            best = np.argmax(all_avg_scores)
            res = (all_subsets[best],
                   all_avg_scores[best],
                   all_cv_scores[best])
        return res

    def _exclusion(self, feature_set, X, y, fixed_feature=None):
        n = len(feature_set)
        res = (None, None, None)
        if n > 1:
            all_avg_scores = []
            all_cv_scores = []
            all_subsets = []
            for p in combinations(feature_set, r=n - 1):
                if fixed_feature and fixed_feature not in set(p):
                    continue
                cv_scores = self._calc_score(X, y, p)
                all_avg_scores.append(cv_scores.mean())
                all_cv_scores.append(cv_scores)
                all_subsets.append(p)
            best = np.argmax(all_avg_scores)
            res = (all_subsets[best],
                   all_avg_scores[best],
                   all_cv_scores[best])
        return res

    def transform(self, X):
        """Reduce X to its most important features.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        Returns
        -------
        Reduced feature subset of X, shape={n_samples, k_features}

        """
        check_is_fitted(self, 'k_feature_idx_')
        return X[:, self.k_feature_idx_]

    def fit_transform(self, X, y):
        """Fit to training data then reduce X to its most important features.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training vectors, where n_samples is the number of samples and
            n_features is the number of features.

        Returns
        -------
        Reduced feature subset of X, shape={n_samples, k_features}

        """
        self.fit(X, y)
        return self.transform(X)


def _name_estimators(estimators):
    """Generate names for estimators."""

    names = [type(estimator).__name__.lower() for estimator in estimators]
    namecount = defaultdict(int)
    for est, name in zip(estimators, names):
        namecount[name] += 1

    for k, v in list(six.iteritems(namecount)):
        if v == 1:
            del namecount[k]

    for i in reversed(range(len(estimators))):
        name = names[i]
        if name in namecount:
            names[i] += "-%d" % namecount[name]
            namecount[name] -= 1

    return list(zip(names, estimators))
