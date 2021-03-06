"""
Tests for module facet.validation
"""
import warnings
from typing import List

import numpy as np
import pytest
from sklearn import datasets, svm, tree
from sklearn.model_selection import GridSearchCV

from sklearndf import __sklearn_0_22__, __sklearn_version__

from facet.validation import BootstrapCV


def test_bootstrap_cv_init() -> None:
    # filter out warnings triggered by sk-learn/numpy

    warnings.filterwarnings("ignore", message="numpy.dtype size changed")
    warnings.filterwarnings("ignore", message="numpy.ufunc size changed")

    # check erroneous inputs
    #   - n_splits = 0
    with pytest.raises(expected_exception=ValueError):
        BootstrapCV(n_splits=0)

    #   - n_splits < 0
    with pytest.raises(expected_exception=ValueError):
        BootstrapCV(n_splits=-1)


def test_get_train_test_splits_as_indices() -> None:

    n_test_splits = 200
    test_x = np.arange(0, 1000, 1)

    my_cv = BootstrapCV(n_splits=n_test_splits, random_state=42)

    def _generate_splits() -> List[np.ndarray]:
        return [test_split for _, test_split in my_cv.split(X=test_x)]

    list_of_test_splits = _generate_splits()

    # assert we get right amount of splits
    assert len(list_of_test_splits) == n_test_splits

    # check average ratio of test/train
    average_test_size = (
        sum(len(test_set) for test_set in list_of_test_splits) / n_test_splits
    )

    assert 0.35 < average_test_size / len(test_x) < 0.37

    list_of_test_splits_2 = _generate_splits()

    assert len(list_of_test_splits) == len(
        list_of_test_splits_2
    ), "the number of splits should be stable"

    for f1, f2 in zip(list_of_test_splits, list_of_test_splits_2):
        assert np.array_equal(f1, f2), "split indices should be stable"


def test_bootstrap_cv_with_sk_learn() -> None:
    # filter out warnings triggered by sk-learn/numpy

    warnings.filterwarnings("ignore", message="numpy.dtype size changed")
    warnings.filterwarnings("ignore", message="numpy.ufunc size changed")

    # load example data
    iris = datasets.load_iris()

    # define a yield-engine circular CV:
    my_cv = BootstrapCV(n_splits=50)

    # define parameters and pipeline
    parameters = {"kernel": ("linear", "rbf"), "C": [1, 10]}
    svc = svm.SVC(gamma="scale")

    # use the defined my_cv bootstrap CV within GridSearchCV:
    if __sklearn_version__ < __sklearn_0_22__:
        clf = GridSearchCV(svc, parameters, cv=my_cv, iid=False)
    else:
        clf = GridSearchCV(svc, parameters, cv=my_cv)
    clf.fit(iris.data, iris.target)

    # test if the number of received splits is correct:
    assert (
        clf.n_splits_ == 50
    ), "50 splits should have been generated by the bootstrap CV"

    assert clf.best_score_ > 0.85, "Expected a minimum score of 0.85"

    # define new parameters and a different pipeline
    # use the defined my_cv circular CV again within GridSeachCV:
    parameters = {
        "criterion": ("gini", "entropy"),
        "max_features": ["sqrt", "auto", "log2"],
    }
    cl2 = GridSearchCV(tree.DecisionTreeClassifier(), parameters, cv=my_cv)
    cl2.fit(iris.data, iris.target)

    assert cl2.best_score_ > 0.85, "Expected a minimum score of 0.85"
