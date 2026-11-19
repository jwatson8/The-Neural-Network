"""Pure reranking tests with synthetic scores, never production outcomes."""

import unittest
from decimal import Decimal
from dataclasses import FrozenInstanceError

from src.experimentation.cost_reranking import (
    CostAwareConfig, RerankingUnavailable, ScoredCandidate, reference_price,
    rerank, treatment_version,
)


class CostRerankingTests(unittest.TestCase):
    def candidates(self, count=20):
        return [ScoredCandidate(f'm{index}', float(count - index)) for index in range(count)]

    def test_higher_prices_receive_larger_penalties(self):
        candidates = [ScoredCandidate('cheap', 1), ScoredCandidate('costly', 1)]
        result = rerank(candidates, {'cheap': '0.1', 'costly': '10'}, k=2)
        cheap, costly = result.selected
        self.assertLess(cheap.normalized_cost, costly.normalized_cost)
        self.assertGreater(cheap.adjusted_score, costly.adjusted_score)

    def test_zero_weight_preserves_model_relevance_and_ties(self):
        candidates = [ScoredCandidate('b', -1), ScoredCandidate('a', 2), ScoredCandidate('c', 2)]
        result = rerank(candidates, dict(a='100', b='0', c='1'), k=3, config=CostAwareConfig(0))
        self.assertEqual([row.movie_id for row in result.selected], ['a', 'c', 'b'])

    def test_positive_weight_changes_selection_and_increasing_weight_prefers_cost(self):
        candidates = self.candidates()
        prices = {item.movie_id: ('1000' if index < 3 else '0.1') for index, item in enumerate(candidates)}
        picks = [rerank(candidates, prices, k=3, config=CostAwareConfig(weight)) for weight in (0, '.15', '1')]
        totals = [sum(item.reference_cost for item in result.selected) for result in picks]
        self.assertLess(totals[1], totals[0])
        self.assertLessEqual(totals[2], totals[1])
        self.assertEqual(len(picks[0].candidates), 20)
        self.assertNotEqual(picks[0].selected, picks[1].selected)

    def test_tie_breaking_is_stable_and_deterministic(self):
        candidates = [ScoredCandidate('b', 1), ScoredCandidate('a', 1), ScoredCandidate('c', 1)]
        first = rerank(candidates, dict(a=1, b=1, c=1), k=2)
        self.assertEqual(first, rerank(candidates, dict(a=1, b=1, c=1), k=2))
        self.assertEqual([item.movie_id for item in first.selected], ['b', 'a'])
        self.assertTrue(all(item.relevance == .5 for item in first.candidates))

    def test_duplicates_exclusions_invalid_ids_and_missing_catalog_rows_are_filtered(self):
        candidates = [ScoredCandidate('watched', 100), ScoredCandidate('absent', 99),
                      ScoredCandidate('bad\nmovie', 98), ScoredCandidate('a', 1),
                      ScoredCandidate('a', 3), ScoredCandidate('b', 2)]
        result = rerank(candidates, {'watched': 1, 'bad\nmovie': 1, 'a': 1, 'b': 1},
                        k=2, exclude_movie_ids={'watched'})
        self.assertEqual([item.movie_id for item in result.selected], ['a', 'b'])
        self.assertEqual(result.selected[0].raw_score, 3)

    def test_missing_invalid_and_nonfinite_prices_are_maximally_penalized(self):
        invalid = [None, '', 'bad', '-1', float('nan'), float('inf'), Decimal('NaN'), True, {}]
        candidates = [ScoredCandidate('known', 1)] + [ScoredCandidate(str(i), 1) for i in range(len(invalid))]
        prices = dict(known='1', **{str(i): cost for i, cost in enumerate(invalid)})
        result = rerank(candidates, prices, k=len(candidates))
        self.assertEqual(result.selected[0].movie_id, 'known')
        self.assertEqual(result.unknown_cost_count, len(invalid))
        self.assertTrue(all(item.normalized_cost == 1 for item in result.candidates if item.movie_id != 'known'))
        self.assertTrue(all(item.reference_cost is None for item in result.candidates if item.movie_id != 'known'))

    def test_all_unknown_prices_cannot_fulfill_treatment(self):
        with self.assertRaisesRegex(RerankingUnavailable, 'no known'):
            rerank(self.candidates(2), {'m0': None, 'm1': '-1'}, k=2)

    def test_equal_prices_and_explicit_zero_prices_are_safe(self):
        for price, expected in (('1', .5), ('0', 0)):
            result = rerank(self.candidates(3), {f'm{i}': price for i in range(3)}, k=2)
            self.assertEqual([item.movie_id for item in result.selected], ['m0', 'm1'])
            self.assertTrue(all(item.normalized_cost == expected for item in result.candidates))
        self.assertEqual(reference_price('0'), Decimal('0'))
        self.assertIsNone(reference_price(None))

    def test_extreme_outlier_does_not_flatten_other_cost_differences(self):
        result = rerank(self.candidates(5), dict(m0='1', m1='2', m2='3', m3='4', m4='1e300'), k=5)
        costs = {item.movie_id: item.normalized_cost for item in result.candidates}
        self.assertGreater(costs['m0'], .1)
        self.assertLess(costs['m0'], costs['m1'])
        self.assertLess(costs['m1'], costs['m4'])
        self.assertEqual(costs['m4'], 1)

    def test_nonfinite_scores_are_not_candidates(self):
        candidates = self.candidates(2) + [ScoredCandidate('bad', float('nan')),
            ScoredCandidate('infinite', float('inf')), ScoredCandidate('boolean', True)]
        result = rerank(candidates, dict(m0=1, m1=1, bad=1, infinite=1, boolean=1), k=2)
        self.assertEqual(len(result.candidates), 2)

    def test_sparse_empty_or_oversized_pools_fail_explicitly(self):
        for candidates in ([], self.candidates(1)):
            with self.assertRaises(RerankingUnavailable):
                rerank(candidates, {'m0': 1}, k=2)
        with self.assertRaises(ValueError):
            rerank(self.candidates(201), {f'm{i}': 1 for i in range(201)}, k=2)

    def test_settings_are_bounded_immutable_and_versioned_with_model_pin(self):
        for weight in ('NaN', 'Infinity', '-1', '1.1', True, 'bad'):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                CostAwareConfig(weight)
        for kwargs in ({'candidate_multiplier': 0}, {'candidate_multiplier': 11},
                       {'candidate_multiplier': True}, {'max_candidates': 201}, {'max_candidates': 1}):
            with self.assertRaises(ValueError):
                CostAwareConfig(**kwargs)
        config = CostAwareConfig()
        self.assertEqual(config.candidate_limit(20), 100)
        self.assertEqual(CostAwareConfig(candidate_multiplier=10).candidate_limit(20), 200)
        with self.assertRaises(FrozenInstanceError):
            config.cost_weight = Decimal('1')
        version = treatment_version('underlying-v1', config)
        self.assertEqual(version, treatment_version('underlying-v1', CostAwareConfig('.150')))
        self.assertNotEqual(version, treatment_version('underlying-v2', config))
        self.assertNotEqual(version, treatment_version('underlying-v1', CostAwareConfig('.2')))


    def test_offline_comparison_reports_coverage_overlap_and_unknown_prices(self):
        from src.experimentation.cost_diagnostics import compare_candidates
        candidates = self.candidates()
        prices = {item.movie_id: ('1000' if i < 3 else '.1') for i, item in enumerate(candidates)}
        report = compare_candidates(candidates, prices, route='synthetic', k=3)
        self.assertLess(report['treatment']['average_reference_cost'], report['control']['average_reference_cost'])
        self.assertEqual(report['control']['valid_recommendations'], 3)
        self.assertEqual(report['changed_recommendations'], 3-report['overlap_count'])
        self.assertLessEqual(report['mean_raw_relevance_difference'], 0)
        prices['m0'] = None
        report = compare_candidates(candidates, prices, route='synthetic', k=3, config=CostAwareConfig(0))
        self.assertIsNone(report['control']['average_reference_cost'])
        self.assertEqual(report['control']['priced_recommendations'], 2)


    def test_synthetic_diagnostics_are_reproducible_and_never_open_database(self):
        from unittest.mock import patch
        from src.experimentation.cost_diagnostics import synthetic_diagnostic
        with patch('src.database.database.get_engine', side_effect=AssertionError('offline diagnostics must not connect')):
            first, second = synthetic_diagnostic(), synthetic_diagnostic()
        self.assertEqual(first, second)
        self.assertTrue(first['diagnostic_only'])
        self.assertEqual(first['route_coverage'], {'collaborative_svd': 1, 'llm_content': 1, 'popularity_fallback': 1})
        self.assertTrue(all(case['control']['valid_recommendations'] == 5 for case in first['cases']))
        self.assertTrue(all(case['treatment']['valid_recommendations'] == 5 for case in first['cases']))
