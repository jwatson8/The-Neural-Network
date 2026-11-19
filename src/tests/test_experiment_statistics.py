"""Independent user-level statistical reference and boundary cases."""

import json
import math

import pytest
from scipy import stats

from src.experimentation.statistics import bootstrap_difference, participation, welch


def test_welch_matches_reference_and_ci():
    c, t = [1, 2, 4, 7], [2, 3, 4, 5, 6, 9]
    result = welch(c, t)
    reference = stats.ttest_ind(t, c, equal_var=False)
    assert result['p_value'] == pytest.approx(reference.pvalue)
    assert result['ci95'] == pytest.approx(list(reference.confidence_interval()))
    assert result['difference'] == pytest.approx(sum(t)/len(t)-sum(c)/len(c))
    assert result['control_n'] == 4 and result['treatment_n'] == 6


@pytest.mark.parametrize('c,t,status', [([], [], 'insufficient_data'), ([1], [1, 2], 'insufficient_data'),
    ([1, 2], [], 'insufficient_data'), ([0, 0], [0, 0], 'undefined_zero_variance'),
    ([1, 1], [2, 2], 'undefined_zero_variance')])
def test_undefined_welch_never_invents_p(c, t, status):
    result = welch(c, t)
    assert result['status'] == status
    assert result['p_value'] is None and result['ci95'] is None


def test_nonfinite_missing_and_zero_baseline():
    result = welch([0, 0, None, float('nan')], [1, 2, float('inf'), 'bad'])
    assert result['control_n'] == result['treatment_n'] == 2
    assert result['excluded_control'] == result['excluded_treatment'] == 2
    assert result['relative_difference'] is None
    assert result['status'] == 'ok'
    json.dumps(result, allow_nan=False)


def test_extreme_range_is_unavailable():
    result = welch([1e308, -1e308], [1e308, 1e308])
    assert result['status'] == 'invalid_numeric_range'
    assert result['p_value'] is None
    json.dumps(result, allow_nan=False)


def test_identical_nondegenerate_samples():
    result = welch([1, 2, 3], [1, 2, 3])
    assert result['difference'] == 0 and result['p_value'] == 1
    assert result['ci95'][0] < 0 < result['ci95'][1]


def test_bootstrap_is_seeded_skewed_and_explicitly_degenerate():
    first = bootstrap_difference([0, 0, 0, 1000], [0, 0, 1], repetitions=100, seed=5)
    assert first == bootstrap_difference([0, 0, 0, 1000], [0, 0, 1], repetitions=100, seed=5)
    assert first['status'] == 'ok'
    assert bootstrap_difference([0, 0], [0, 0], repetitions=100)['status'] == 'empirical_degenerate'
    assert bootstrap_difference([], [1], repetitions=100)['ci95'] is None


@pytest.mark.parametrize('kwargs', [{'seed': -1}, {'repetitions': 0}, {'repetitions': 5001}])
def test_invalid_resampling_parameters(kwargs):
    with pytest.raises(ValueError):
        bootstrap_difference([1, 2], [2, 3], **kwargs)


def test_binary_inference_uses_users_and_reference_fisher():
    result = participation([False, True, False, False], [True, True, False])
    assert result['control_n'] == 4 and result['treatment_n'] == 3
    assert result['difference'] == pytest.approx(2/3-1/4)
    assert result['p_value'] == pytest.approx(stats.fisher_exact([[2, 1], [1, 3]]).pvalue)
    assert -1 <= result['ci95'][0] < result['difference'] < result['ci95'][1] <= 1


def test_binary_all_zero_not_certainty_and_missing():
    result = participation([0, 0], [0, 0])
    assert result['p_value'] == 1 and result['relative_difference'] is None
    assert result['ci95'][0] < 0 < result['ci95'][1]
    assert participation([], [1])['p_value'] is None
    assert participation([0, 3, None], [1, float('inf')])['excluded_control'] == 2
