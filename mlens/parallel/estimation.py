"""ML-ENSEMBLE

:author: Sebastian Flennerhag
:copyright: 2017
:licence: MIT

Base class for estimation.
"""

from abc import ABCMeta, abstractmethod
import numpy as np

from ..utils import (safe_print, print_time, pickle_load, pickle_save,
                     check_is_fitted)
from ..utils.exceptions import (FitFailedError, FitFailedWarning,
                                NotFittedError, PredictFailedError,
                                PredictFailedWarning,
                                ParallelProcessingWarning,
                                ParallelProcessingError)

from joblib import delayed
import os

from time import sleep
try:
    from time import perf_counter as time_
except ImportError:
    from time import time as time_

import warnings


class BaseEstimator(object):

    """Base class for estimating a layer in parallel.

    Estimation class to be used as based for a layer estimation engined that
    is callable by the :class:`ParallelProcess` job manager.

    A subclass must implement a ``_format_instance_list`` method for
    building a list of preprocessing cases and a list of estimators that
    will be iterated over in the call to :class:`joblib.Parallel`,
    and a ``_get_col_id`` method for assigning a unique column and if
    applicable, row slice, to each estimator in the estimator list.
    The subclass ``__init__`` method should be a call to ``super``.

    Parameters
    ----------
    layer : :class:`Layer`
        layer to be estimated

    dual : bool
        whether to estimate transformers separately from estimators: else,
        the lists will be combined in one parallel for-loop.
    """

    __metaclass__ = ABCMeta

    __slots__ = ['verbose', 'layer', 'raise_', 'name', 'classes', 'proba',
                 'ivals', 'dual', 'e', 't', 'c', 'scorer']

    @abstractmethod
    def __init__(self, layer, dual=True):
        self.layer = layer

        # Copy some layer parameters to ease notation
        self.verbose = self.layer.verbose
        self.raise_ = self.layer.raise_on_exception
        self.name = self.layer.name
        self.proba = self.layer.proba
        self.scorer = self.layer.scorer
        self.ivals = (getattr(layer, 'ival', 0.1), getattr(layer, 'lim', 600))

        # Set estimator and transformer lists to loop over, and collect
        # estimator column ids for the prediction matrix
        self.e, self.t = self._format_instance_list()
        self.c = self._get_col_id()

        self.dual = dual

    def __call__(self, attr, *args, **kwargs):
        """Generic argument agnostic call function to a Stacker method."""
        getattr(self, attr)(*args, **kwargs)

    @abstractmethod
    def _format_instance_list(self):
        """Formatting layer's estimator and preprocessing for parallel loop."""

    @abstractmethod
    def _get_col_id(self):
        """Assign unique col_id to every estimator."""

    def _assemble(self, dir):
        """Store fitted transformer and estimators in the layer."""
        self.layer.preprocessing_ = _assemble(dir, self.t, 't')
        self.layer.estimators_, s = _assemble(dir, self.e, 'e')

        if self.scorer is not None and self.layer.cls is not 'full':
            self.layer.scores_ = self._build_scores(s)

    def _build_scores(self, s):
        """Build a cv-score mapping."""
        scores = dict()

        # Build shell dictionary with main estimators as keys
        for k, v in s[:self.layer.n_pred]:
            case_name, est_name = k.split('___')

            if case_name == '':
                name = est_name
            else:
                name = '%s__%s' % (case_name, est_name)

            scores[name] = []

        # Populate with list of scores from folds
        for k, v in s[self.layer.n_pred:]:
            case_name, est_name = k.split('___')

            est_name = '__'.join(est_name.split('__')[:-1])

            if '__' not in case_name:
                name = est_name
            else:
                case_name = case_name.split('__')[0]
                name = '%s__%s' % (case_name, est_name)

            scores[name].append(v)

        # Aggregate to get cross-validated mean scores
        for k, v in scores.items():
            scores[k] = _mean_score(v, self.raise_, k, self.name)

        return scores

    def fit(self, X, y, P, dir, parallel):
        """Fit layer through given attribute."""
        if self.verbose:
            printout = "stderr" if self.verbose < 50 else "stdout"
            s = _name(self.name, None)
            safe_print('Fitting %s' % self.name)
            t0 = time_()

        pred_method = 'predict' if not self.proba else 'predict_proba'
        preprocess = self.t is not None

        if y.shape[0] > X.shape[0]:
            # This is legal if X is a prediction matrix generated by predicting
            # only a subset of the original training set.
            # Since indexing is strictly monotonic, we can simply discard
            # the first observations y to get the corresponding labels.
            rebase = y.shape[0] - X.shape[0]
            y = y[rebase:]

        if self.dual:
            if preprocess:
                parallel(delayed(fit_trans)(dir=dir,
                                            case=case,
                                            inst=instance_list,
                                            X=X,
                                            y=y,
                                            idx=tri,
                                            name=self.name)
                         for case, tri, _, instance_list in self.t)

            parallel(delayed(fit_est)(dir=dir,
                                      case=case,
                                      inst_name=inst_name,
                                      inst=instance,
                                      X=X,
                                      y=y,
                                      pred=P if tei is not None else None,
                                      idx=(tri, tei, self.c[case, inst_name]),
                                      name=self.name,
                                      raise_on_exception=self.raise_,
                                      preprocess=preprocess,
                                      ivals=self.ivals,
                                      attr=pred_method,
                                      scorer=self.scorer)
                     for case, tri, tei, instance_list in self.e
                     for inst_name, instance in instance_list)

        else:
            parallel(delayed(_fit)(dir=dir,
                                   case=case,
                                   inst_name=inst_name,
                                   inst=instance,
                                   X=X,
                                   y=y,
                                   pred=P if tei is not None else None,
                                   idx=(tri, tei, self.c[case, inst_name])
                                   if inst_name != '__trans__' else tri,
                                   name=self.layer.name,
                                   raise_on_exception=self.raise_,
                                   preprocess=preprocess,
                                   ivals=self.ivals,
                                   scorer=self.scorer)
                     for case, tri, tei, inst_list in _wrap(self.t) + self.e
                     for inst_name, instance in inst_list)

        # Load instances from cache and store as layer attributes
        # Typically, as layer.estimators_, layer.preprocessing_
        self._assemble(dir)

        if self.verbose:
            print_time(t0, '%sDone' % s, file=printout)

    def predict(self, X, P, parallel):
        """Predict with fitted layer with either full or fold ests."""
        self._check_fitted()

        if self.verbose:
            printout = "stderr" if self.verbose < 50 else "stdout"
            s = _name(self.name, None)
            safe_print('Predicting %s' % self.name)
            t0 = time_()

        pred_method = 'predict' if not self.proba else 'predict_proba'

        # Collect estimators, either fitted on full data or folds
        prep, ests = self._retrieve('full')

        parallel(delayed(predict_est)(case=case,
                                      tr_list=prep[case]
                                      if prep is not None else [],
                                      inst_name=inst_name,
                                      est=est,
                                      xtest=X,
                                      pred=P,
                                      col=col,
                                      name=self.name,
                                      attr=pred_method)
                 for case, (inst_name, est, (_, col)) in ests)

        if self.verbose:
            print_time(t0, '%sDone' % s, file=printout)

    def transform(self, X, P, parallel):
        """Transform training data with fold-estimators from fit call."""
        self._check_fitted()

        if self.verbose:
            printout = "stderr" if self.verbose < 50 else "stdout"
            s = _name(self.name, None)
            safe_print('Predicting %s' % self.name)
            t0 = time_()

        pred_method = 'predict' if not self.proba else 'predict_proba'

        # Collect estimators, either fitted on full data or folds
        prep, ests = self._retrieve('fold')

        parallel(delayed(predict_fold_est)(case=case,
                                           tr_list=prep[case]
                                           if prep is not None else [],
                                           inst_name=est_name,
                                           est=est,
                                           xtest=X,
                                           pred=P,
                                           idx=idx,
                                           name=self.name,
                                           attr=pred_method)
                 for case, (est_name, est, idx) in ests)

        if self.verbose:
            print_time(t0, '%sDone' % s, file=printout)

    def _check_fitted(self):
        """Utility function for checking that fitted estimators exist."""
        check_is_fitted(self.layer, "estimators_")

        # Check that there is at least one fitted estimator
        if isinstance(self.layer.estimators_, (list, tuple, set)):
            empty = len(self.layer.estimators_) == 0
        elif isinstance(self.layer.estimators_, dict):
            empty = any([len(e) == 0 for e in self.layer.estimators_.values()])
        else:
            # Cannot determine shape of estimators, skip check
            return

        if empty:
            raise NotFittedError("Cannot predict as no estimators were"
                                 "successfully fitted.")

    def _retrieve(self, s):
        """Get transformers and estimators fitted on folds or on full data."""
        n_pred = self.layer.n_pred
        n_prep = max(self.layer.n_prep, 1)

        if s == 'full':
            # If full, grab the first n_pred estimators, and the first
            # n_prep preprocessing pipelines, which are fitted on
            # the full training data. We take max on n_prep to avoid getting
            # empty preprocessing_ slice when n_prep = 0 when no preprocessing.
            ests = self.layer.estimators_[:n_pred]

            if self.layer.preprocessing_ is None:
                prep = None
            else:
                prep = dict(self.layer.preprocessing_[:n_prep])

        elif s == 'fold':
            # If fold, grab the estimators after n_pred, and the preprocessing
            # pipelines after n_prep, which are fitted on folds of the
            # training data.
            ests = self.layer.estimators_[n_pred:]

            if self.layer.preprocessing_ is None:
                prep = None
            else:
                prep = dict(self.layer.preprocessing_[n_prep:])

        else:
            raise ValueError("Argument not understood. Only 'full' and 'fold' "
                             "are acceptable argument values.")

        return prep, ests


###############################################################################
def _wrap(folded_list, name='__trans__'):
    """Wrap the folded transformer list.

    wraps a folded transformer list so that the ``tr_list`` appears as
    one estimator with a specified name. Since all ``tr_list``s have the
    same name, it can be used to select a transformation function or an
    estimation function in a combined parallel fitting loop.
    """
    return [(case, tri, None, [(name, instance_list)]) for
            case, tri, tei, instance_list in folded_list]


def _strip(cases, fitted_estimators):
    """Strip all estimators not fitted on full data from list."""
    return [tup for tup in fitted_estimators if tup[0] in cases]


def _name(layer_name, case):
    """Utility for setting error or warning message prefix."""
    if layer_name is None and case is None:
        # Both empty
        out = ''
    elif layer_name is not None and case is not None:
        # Both full
        out = '[%s | %s ] ' % (layer_name, case)
    elif case is None:
        # Case empty, layer_name full
        out = '[%s] ' % layer_name
    else:
        # layer_name empty, case full
        out = '[%s] ' % case
    return out


def _slice_array(x, y, idx):
    """Build training array index and slice data."""
    # Have to be careful in prepping data for estimation.
    # We need to slice memmap and convert to a proper array - otherwise
    # transformers can store results memmaped to the cache, which will
    # prevent the garbage collector from releasing the memmaps from memory
    # after estimation
    if idx is None:
        idx = None
    else:
        if isinstance(idx[0], tuple):
            # If a tuple of indices, build iteratively
            idx = np.hstack([np.arange(t0, t1) for t0, t1 in idx])
        else:
            idx = np.arange(idx[0], idx[1])

    x = x[idx] if idx is not None else x

    if y is not None:
        y = np.asarray(y[idx]) if idx is not None else np.asarray(y)

    if x.__class__.__name__[:3] not in ['csr', 'csc', 'coo', 'dok']:
        # numpy asarray does not work with scipy sparse. Current experimental
        # solution is to just leave them as is.
        x = np.asarray(x)

    return x, y, idx


def _assemble(dir, instance_list, suffix):
    """Utility for loading fitted instances."""
    if suffix is 't':
        if instance_list is None:
            return

        return [(tup[0],
                 pickle_load(os.path.join(dir, '%s__%s' % (tup[0], suffix))))
                for tup in instance_list]
    else:
        # We iterate over estimators to split out the estimator info and the
        # scoring info (if any)
        ests_ = []
        scores_ = []
        for tup in instance_list:
            for etup in tup[-1]:
                f = os.path.join(dir, '%s__%s__%s' % (tup[0], etup[0], suffix))
                loaded = pickle_load(f)

                # split out the scores, the final element in the l tuple
                ests_.append((tup[0], loaded[:-1]))

                case = '%s___' % tup[0] if tup[0] is not None else '___'
                scores_.append((case + etup[0], loaded[-1]))

        return ests_, scores_


###############################################################################
def predict_est(case, tr_list, inst_name, est, xtest, pred, col, name, attr):
    """Method for predicting with fitted transformers and estimators."""
    # Transform input
    for tr_name, tr in tr_list:
        xtest = _transform_tr(xtest, tr, tr_name, case, name)

    # Predict into memmap
    # Here, we coerce errors on failed predictions - all predictors that
    # survive into the estimators_ attribute of a layer should be able to
    # predict, otherwise the subsequent layer will get corrupt input.
    p = _predict_est(xtest, est, True, inst_name, case, name, attr)

    if len(p.shape) == 1:
        pred[:, col] = p
    else:
        pred[:, np.arange(col, col + p.shape[1])] = p


def predict_fold_est(case, tr_list, inst_name, est, xtest, pred, idx, name,
                     attr):
    """Method for predicting with transformers and estimators from fit call."""
    tei = idx[0]
    col = idx[1]

    x, _, tei = _slice_array(xtest, None, tei)

    for tr_name, tr in tr_list:
        x = _transform_tr(x, tr, tr_name, case, name)

    # Predict into memmap
    # Here, we coerce errors on failed predictions - all predictors that
    # survive into the estimators_ attribute of a layer should be able to
    # predict, otherwise the subsequent layer will get corrupt input.
    p = _predict_est(x, est, True, inst_name, case, name, attr)

    rebase = xtest.shape[0] - pred.shape[0]
    tei -= rebase

    if len(p.shape) == 1:
        pred[tei, col] = p
    else:
        cols = np.arange(col, col + p.shape[1])
        pred[np.ix_(tei, cols)] = p


def fit_trans(dir, case, inst, X, y, idx, name):
    """Fit transformers and write to cache."""
    x, y, _ = _slice_array(X, y, idx)

    out = []
    for tr_name, tr in inst:
        # Fit transformer
        tr = _fit_tr(x, y, tr, tr_name, case, name)

        # If more than one step, transform input for next step
        if len(inst) > 1:
            x = _transform_tr(x, tr, tr_name, case, name)
        out.append((tr_name, tr))

    # Write transformer list to cache
    f = os.path.join(dir, '%s__t' % case)
    pickle_save(out, f)


def fit_est(dir, case, inst_name, inst, X, y, pred, idx, raise_on_exception,
            preprocess, name, ivals, attr, scorer=None):
    """Fit estimator and write to cache along with predictions."""
    # Have to be careful in prepping data for estimation.
    # We need to slice memmap and convert to a proper array - otherwise
    # estimators can store results memmaped to the cache, which will
    # prevent the garbage collector from releasing the memmaps from memory
    # after estimation
    x, z, _ = _slice_array(X, y, idx[0])

    # Load transformers
    if preprocess:
        f = os.path.join(dir, '%s__t' % case)
        tr_list = _load_trans(f, case, ivals, raise_on_exception)
    else:
        tr_list = []

    # Transform input
    for tr_name, tr in tr_list:
        x = _transform_tr(x, tr, tr_name, case, name)

    # Fit estimator
    est = _fit_est(x, z, inst, raise_on_exception, inst_name, case, name)

    # Predict if asked
    # The predict loop is kept separate to allow overwrite of x, thus keeping
    # only one subset of X in memory at any given time
    if idx[1] is not None:
        tei = idx[1]
        col = idx[2]

        x, z, tei = _slice_array(X, y, tei)

        for tr_name, tr in tr_list:
            x = _transform_tr(x, tr, tr_name, case, name)

        p = _predict_est(x, est, raise_on_exception,
                         inst_name, case, name, attr)

        rebase = X.shape[0] - pred.shape[0]
        tei -= rebase

        if len(p.shape) == 1:
            pred[tei, col] = p
        else:
            cols = np.arange(col, col + p.shape[1])
            pred[np.ix_(tei, cols)] = p

        try:
            s = scorer(z, p)
        except Exception:
            s = None

    # We drop tri from index and only keep tei if any predictions were made
        idx = idx[1:]
    else:
        idx = (None, idx[2])
        s = None

    f = os.path.join(dir, '%s__%s__e' % (case, inst_name))
    pickle_save((inst_name, est, idx, s), f)


def _fit(**kwargs):
    """Wrapper to select fit_est or fit_trans."""
    f = fit_trans if kwargs['inst_name'] == '__trans__' else fit_est
    f(**{k: v for k, v in kwargs.items() if k in f.__code__.co_varnames})


###############################################################################
def _load_trans(dir, case, ivals, raise_on_exception):
    """Try loading transformers, and handle exception if not ready yet."""
    s = ivals[0]
    lim = ivals[1]
    try:
        # Assume file exists
        return pickle_load(dir)
    except (FileNotFoundError, TypeError) as exc:
        msg = str(exc)
        error_msg = ("The file %s cannot be found after %i seconds of "
                     "waiting. Check that time to fit transformers is "
                     "sufficiently fast to complete fitting before "
                     "fitting estimators. Consider reducing the "
                     "preprocessing intensity in the ensemble, or "
                     "increase the '__lim__' attribute to wait extend "
                     "period of waiting on transformation to complete."
                     " Details:\n%r")

        if raise_on_exception:
            # Raise error immediately
            raise ParallelProcessingError(error_msg % msg)

        # Else, check intermittently until limit is reached
        ts = time_()
        while not os.path.exists(dir):
            sleep(s)
            if time_() - ts > lim:
                if raise_on_exception:
                    raise ParallelProcessingError(error_msg % msg)

                warnings.warn("Transformer %s not found in cache (%s). "
                              "Will check every %.1f seconds for %i seconds "
                              "before aborting. " % (case, dir, s, lim),
                              ParallelProcessingWarning)

                raise_on_exception = True
                ts = time_()

        return pickle_load(dir)


def _fit_tr(x, y, tr, tr_name, case, layer_name):
    """Wrapper around try-except block for fitting transformer."""
    try:
        return tr.fit(x, y)
    except Exception as e:
        # Transformation is sequential: always throw error if one fails
        s = _name(layer_name, case)
        msg = "%sFitting transformer [%s] failed. Details:\n%r"
        raise FitFailedError(msg % (s, tr_name, e))


def _transform_tr(x, tr, tr_name, case, layer_name):
    """Wrapper around try-except block for transformer transformation."""
    try:
        return tr.transform(x)
    except Exception as e:
        s = _name(layer_name, case)
        msg = "%sTransformation with transformer [%s] of type (%s) failed. " \
              "Details:\n%r"
        raise FitFailedError(msg % (s, tr_name, tr.__class__, e))


def _fit_est(x, y, est, raise_on_exception, inst_name, case, layer_name):
    """Wrapper around try-except block for estimator fitting."""
    try:
        return est.fit(x, y)
    except Exception as e:
        s = _name(layer_name, case)

        if raise_on_exception:
            raise FitFailedError("%sCould not fit estimator '%s'. "
                                 "Details:\n%r" % (s, inst_name, e))

        msg = "%sCould not fit estimator '%s'. Will drop from " \
              "ensemble. Details:\n%r"
        warnings.warn(msg % (s, inst_name, e), FitFailedWarning)


def _predict_est(x, est, raise_on_exception, inst_name, case, name, attr):
    """Wrapper around try-except block for estimator predictions."""
    try:
        return getattr(est, attr)(x)
    except Exception as e:
        s = _name(name, case)

        if raise_on_exception:
            raise PredictFailedError("%sCould not call '%s' with estimator "
                                     "'%s'. Details:\n"
                                     "%r" % (s, attr, inst_name, e))

        msg = "%sCould not call '%s' with estimator '%s'. Predictions set " \
              "to 0. Details:\n%r"
        warnings.warn(msg % (s, attr, inst_name, e), PredictFailedWarning)


def _mean_score(v, raise_, k, layer_name):
    """Exception handling wrapper for getting the mean of list of scores."""
    try:
        return np.mean(v), np.std(v)
    except Exception as e:
        s = _name(layer_name, None)
        msg = "%sCould not score instance %s. Details\n%r" % (s, k, e)
        if raise_:
            raise ParallelProcessingError(msg)
        else:
            warnings.warn(msg, ParallelProcessingWarning)
