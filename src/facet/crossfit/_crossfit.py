"""
Core implementation of :mod:`facet.crossfit`
"""

import logging
from abc import ABCMeta
from copy import copy
from typing import (
    Callable,
    Container,
    Generic,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

import numpy as np
import pandas as pd
from numpy.random.mtrand import RandomState
from sklearn.base import BaseEstimator
from sklearn.metrics import check_scoring
from sklearn.model_selection import BaseCrossValidator
from sklearn.utils import check_random_state

from pytools.api import AllTracker, inheritdoc
from pytools.fit import FittableMixin
from pytools.parallelization import ParallelizableMixin
from sklearndf import LearnerDF, TransformerDF
from sklearndf.pipeline import (
    ClassifierPipelineDF,
    LearnerPipelineDF,
    RegressorPipelineDF,
)

from facet.data import Sample

log = logging.getLogger(__name__)

__all__ = ["LearnerCrossfit", "Scorer"]

#
# Type variables
#

T_Self = TypeVar("T_Self")
T_LearnerPipelineDF = TypeVar("T_LearnerPipelineDF", bound=LearnerPipelineDF)
T_ClassifierPipelineDF = TypeVar("T_ClassifierPipelineDF", bound=ClassifierPipelineDF)
T_RegressorPipelineDF = TypeVar("T_RegressorPipelineDF", bound=RegressorPipelineDF)

#
# Ensure all symbols introduced below are included in __all__
#

__tracker = AllTracker(globals())

#
# Type definitions
#


#: a scorer generated by :func:`sklearn.metrics.make_scorer`
Scorer = Callable[
    [
        # trained learner to use for scoring
        BaseEstimator,
        # test data that will be fed to the learner
        pd.DataFrame,
        # target values for X
        Union[pd.Series, pd.DataFrame],
        # sample weights
        Optional[pd.Series],
    ],
    # result of applying score function to estimator applied to X
    float,
]

#
# Class definitions
#


class _FitScoreParameters(NamedTuple):
    pipeline: T_LearnerPipelineDF

    # fit parameters
    train_features: Optional[pd.DataFrame]
    train_feature_sequence: Optional[pd.Index]
    train_target: Union[pd.Series, pd.DataFrame, None]
    train_weight: Optional[pd.Series]

    # score parameters
    scorer: Optional[Scorer]
    score_train_split: bool
    test_features: Optional[pd.DataFrame]
    test_target: Union[pd.Series, pd.DataFrame, None]
    test_weight: Optional[pd.Series]


@inheritdoc(match="[see superclass]")
class LearnerCrossfit(
    FittableMixin[Sample],
    ParallelizableMixin,
    Generic[T_LearnerPipelineDF],
    metaclass=ABCMeta,
):
    """
    Fits a learner pipeline to all train splits of a given cross-validation strategy,
    with optional feature shuffling.

    Feature shuffling can be helpful when fitting models with a data set that contains
    very similar features.
    For such groups of similar features, some learners may pick features based on their
    relative position in the training data table.
    Feature shuffling randomizes the sequence of features for each cross-validation
    training sample, thus ensuring that all similar features have the same chance of
    being used across crossfits.

    Feature shuffling is active by default, so that every model is trained on a random
    permutation of the feature columns to avoid favouring one of several similar
    features based on column sequence.
    """

    __NO_SCORING = "<no scoring>"

    def __init__(
        self,
        pipeline: T_LearnerPipelineDF,
        cv: BaseCrossValidator,
        *,
        shuffle_features: Optional[bool] = None,
        random_state: Union[int, RandomState, None] = None,
        n_jobs: Optional[int] = None,
        shared_memory: Optional[bool] = None,
        pre_dispatch: Optional[Union[str, int]] = None,
        verbose: Optional[int] = None,
    ) -> None:
        """
        :param pipeline: learner pipeline to be fitted
        :param cv: the cross-validator generating the train splits
        :param shuffle_features: if ``True``, shuffle column order of features for
            every crossfit (default: ``False``)
        :param random_state: optional random seed or random state for shuffling the
            feature column order
        """
        super().__init__(
            n_jobs=n_jobs,
            shared_memory=shared_memory,
            pre_dispatch=pre_dispatch,
            verbose=verbose,
        )

        if not isinstance(pipeline, LearnerPipelineDF):
            raise TypeError("arg pipeline must be a LearnerPipelineDF")
        self.pipeline: T_LearnerPipelineDF = pipeline.clone()

        if not hasattr(cv, "split"):
            raise TypeError(
                "arg cv must be a cross-validator implementing method split()"
            )
        self.cv = cv

        self.shuffle_features: bool = (
            False if shuffle_features is None else shuffle_features
        )

        self.random_state = random_state

        self._splits: Optional[List[Tuple[Sequence[int], Sequence[int]]]] = None
        self._model_by_split: Optional[List[T_LearnerPipelineDF]] = None
        self._sample: Optional[Sample] = None

    __init__.__doc__ += ParallelizableMixin.__init__.__doc__

    @property
    def is_fitted(self) -> bool:
        """[see superclass]"""
        return self._sample is not None

    @property
    def n_splits_(self) -> int:
        """
        The number of fits in this crossfit.
        """
        self._ensure_fitted()
        return len(self._model_by_split)

    @property
    def sample_(self) -> Sample:
        """
        The sample used to train this crossfit.
        """
        self._ensure_fitted()
        return self._sample

    def fit(self: T_Self, sample: Sample, **fit_params) -> T_Self:
        """
        Fit the underlying pipeline to the full sample, and fit clones of the pipeline
        to each of the train splits generated by the cross-validator.

        :param sample: the sample to fit the estimators to; if the sample
            weights these are passed on to the learner as keyword argument
            ``sample_weight``
        :param fit_params: optional fit parameters, to be passed on to the fit method
            of the base estimator
        :return: ``self``
        """

        self: LearnerCrossfit  # support type hinting in PyCharm

        # un-fit this instance so we have a defined state in case of an exception
        self._reset_fit()

        self._fit_score(_sample=sample, **fit_params)

        return self

    def score(
        self,
        scoring: Union[str, Callable[[float, float], float], None] = None,
        train_scores: bool = False,
    ) -> np.ndarray:
        """
        Score all models in this crossfit using the given scoring function.

        The crossfit must already be fitted, see :meth:`.fit`.

        :param scoring: scoring to use to score the models (see
            :func:`~sklearn.metrics.check_scoring` for details); if the crossfit
            was fitted using sample weights, these are passed on to the scoring
            function as keyword argument ``sample_weight``
        :param train_scores: if ``True``, calculate train scores instead of test
            scores (default: ``False``)
        :return: the resulting scores as a 1d numpy array
        """

        return self._fit_score(_scoring=scoring, _train_scores=train_scores)

    def fit_score(
        self,
        sample: Sample,
        scoring: Union[str, Callable[[float, float], float], None] = None,
        train_scores: bool = False,
        **fit_params,
    ) -> np.ndarray:
        """
        Fit then score this crossfit.

        See :meth:`.fit` and :meth:`.score` for details.

        :param sample: the sample to fit the estimators to; if the sample
            weights these are passed on to the learner and scoring function as
            keyword argument ``sample_weight``
        :param fit_params: optional fit parameters, to be passed on to the fit method
            of the learner
        :param scoring: scoring function to use to score the models
            (see :func:`~sklearn.metrics.check_scoring` for details)
        :param train_scores: if ``True``, calculate train scores instead of test
            scores (default: ``False``)
        :return: the resulting scores
        """

        # un-fit this instance so we have a defined state in case of an exception
        self._reset_fit()

        return self._fit_score(
            _sample=sample, _scoring=scoring, _train_scores=train_scores, **fit_params
        )

    def resize(self: T_Self, n_splits: int) -> T_Self:
        """
        Reduce the size of this crossfit by removing a subset of the fits.

        :param n_splits: the number of fits to keep. Must be lower, or equal to, the
            current number of fits
        :return: ``self``
        """
        self: LearnerCrossfit

        # ensure that arg n_split has a valid value
        if n_splits > self.n_splits_:
            raise ValueError(
                f"arg n_splits={n_splits} must not be greater than the number of fits"
                f"in the original crossfit ({self.n_splits_} fits)"
            )
        elif n_splits < 1:
            raise ValueError(f"arg n_splits={n_splits} must be a positive integer")

        # copy self and only keep the specified number of fits
        new_crossfit = copy(self)
        new_crossfit._model_by_split = self._model_by_split[:n_splits]
        new_crossfit._splits = self._splits[:n_splits]
        return new_crossfit

    def splits(self) -> Iterator[Tuple[Sequence[int], Sequence[int]]]:
        """
        Get an iterator of all train/test splits used by this crossfit.

        :return: an iterator of all train/test splits used by this crossfit
        """
        self._ensure_fitted()

        # ensure we do not return more splits than we have fitted models
        # this is relevant if this is a resized learner crossfit
        return iter(self._splits)

    def models(self) -> Iterator[T_LearnerPipelineDF]:
        """
        Get an iterator of all models fitted on the cross-validation train splits.

        :return: an iterator of all models fitted on the cross-validation train splits
        """
        self._ensure_fitted()
        return iter(self._model_by_split)

    # noinspection PyPep8Naming
    def _fit_score(
        self,
        _sample: Optional[Sample] = None,
        _scoring: Union[str, Callable[[float, float], float], None] = __NO_SCORING,
        _train_scores: bool = False,
        sample_weight: pd.Series = None,
        **fit_params,
    ) -> Optional[np.ndarray]:

        if sample_weight is not None:
            raise ValueError(
                "do not use arg sample_weight to pass sample weights; "
                "specify a weight column in class Sample instead"
            )

        do_fit = _sample is not None
        do_score = _scoring is not LearnerCrossfit.__NO_SCORING

        assert do_fit or do_score, "at least one of fitting or scoring is enabled"

        pipeline = self.pipeline

        if not do_fit:
            _sample = self.sample_

        sample_weight = _sample.weight

        features = _sample.features
        target = _sample.target

        if do_fit:
            if sample_weight is None:
                pipeline.fit(X=features, y=target, **fit_params)
            else:
                pipeline.fit(
                    X=features, y=target, sample_weight=sample_weight, **fit_params
                )

        # prepare scoring

        scorer: Optional[Scorer]

        if do_score:
            if not isinstance(_scoring, str) and isinstance(_scoring, Container):
                raise ValueError(
                    "Multi-metric scoring is not supported, "
                    "use a single scorer instead; "
                    f"arg scoring={_scoring} was passed"
                )

            scorer = check_scoring(
                estimator=self.pipeline.final_estimator.native_estimator,
                scoring=_scoring,
            )
        else:
            scorer = None

        # calculate the splits: we need to preserve them as we cannot rely on the
        # cross-validator being deterministic

        if do_fit:
            splits: List[Tuple[Sequence[int], Sequence[int]]] = list(
                self.cv.split(X=features, y=target)
            )
        else:
            splits = self._splits

        # generate parameter objects for fitting and/or scoring each split

        def _generate_parameters() -> Iterator[_FitScoreParameters]:
            learner_features = pipeline.feature_names_out_
            n_learner_features = len(learner_features)
            test_scores = do_score and not _train_scores
            models = iter(lambda: None, 0) if do_fit else self.models()
            random_state = check_random_state(self.random_state)
            weigh_samples = sample_weight is not None

            for (train, test), model in zip(splits, models):
                yield _FitScoreParameters(
                    pipeline=pipeline.clone() if do_fit else model,
                    train_features=(
                        features.iloc[train] if do_fit or _train_scores else None
                    ),
                    train_feature_sequence=(
                        learner_features[random_state.permutation(n_learner_features)]
                        if do_fit and self.shuffle_features
                        else None
                    ),
                    train_target=target.iloc[train] if do_fit else None,
                    train_weight=(
                        sample_weight.iloc[train]
                        if weigh_samples and (do_fit or _train_scores)
                        else None
                    ),
                    scorer=scorer,
                    score_train_split=_train_scores,
                    test_features=features.iloc[test] if test_scores else None,
                    test_target=target.iloc[test] if test_scores else None,
                    test_weight=(
                        sample_weight.iloc[test]
                        if weigh_samples and test_scores
                        else None
                    ),
                )

        with self._parallel() as parallel:
            model_and_score_by_split: List[
                Tuple[T_LearnerPipelineDF, Optional[float]]
            ] = parallel(
                self._delayed(LearnerCrossfit._fit_and_score_model_for_split)(
                    parameters, **fit_params
                )
                for parameters in _generate_parameters()
            )

        model_by_split, scores = zip(*model_and_score_by_split)

        if do_fit:
            self._splits = splits
            self._model_by_split = model_by_split
            self._sample = _sample

        return np.array(scores) if do_score else None

    def _reset_fit(self) -> None:
        self._sample = None
        self._splits = None
        self._model_by_split = None

    # noinspection PyPep8Naming
    @staticmethod
    def _fit_and_score_model_for_split(
        parameters: _FitScoreParameters, **fit_params
    ) -> Tuple[Optional[T_LearnerPipelineDF], Optional[float]]:
        do_fit = parameters.train_target is not None
        do_score = parameters.scorer is not None

        pipeline: LearnerPipelineDF

        if do_fit:
            pipeline = parameters.pipeline.fit(
                X=parameters.train_features,
                y=parameters.train_target,
                feature_sequence=parameters.train_feature_sequence,
                sample_weight=parameters.train_weight,
                **fit_params,
            )

        else:
            pipeline = parameters.pipeline

        score: Optional[float]

        if do_score:
            preprocessing: TransformerDF = pipeline.preprocessing
            learner: LearnerDF = pipeline.final_estimator

            if parameters.score_train_split:
                features = parameters.train_features
                target = parameters.train_target
                weight = parameters.train_weight
            else:
                features = parameters.test_features
                target = parameters.test_target
                weight = parameters.test_weight

            if preprocessing:
                features = preprocessing.transform(X=features)

            score = parameters.scorer(
                learner.native_estimator, features, target, weight
            )

        else:
            score = None

        return pipeline if do_fit else None, score

    def __len__(self) -> int:
        return self.n_splits_


__tracker.validate()
