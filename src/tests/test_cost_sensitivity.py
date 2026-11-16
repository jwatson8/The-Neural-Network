"""Offline lambda-grid metrics, fixed inputs, provenance and CLI regression tests."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from src.experimentation.cost_reranking import CostAwareConfig, ScoredCandidate, rerank
from src.experimentation.cost_sensitivity import (
    EvaluationCase, LAMBDA_GRID, analyze, evaluate_case, main, run_sensitivity, summary_table,
)


class CostSensitivityTests(unittest.TestCase):
    def case(self, *, route='collaborative_svd', control=('a', 'b'), candidates=None, prices=None, scores=None):
        return EvaluationCase('fixed-case', route,
            tuple(candidates if candidates is not None else [ScoredCandidate('a', 3), ScoredCandidate('b', 2), ScoredCandidate('c', 1)]),
            prices if prices is not None else {'a': '10', 'b': '1', 'c': '0'}, tuple(control),
            control_scores=scores or {})

    def test_complete_grid_reuses_identical_inputs_and_real_reranker(self):
        calls = []
        def observed(candidates, prices, **kwargs):
            calls.append((id(candidates), id(prices), id(kwargs['exclude_movie_ids']), kwargs['config'].cost_weight))
            return rerank(candidates, prices, **kwargs)
        with patch('src.experimentation.cost_sensitivity.rerank', side_effect=observed):
            report = evaluate_case(self.case(), k=2)
        self.assertEqual([row['lambda_value'] for row in report['rows']], list(LAMBDA_GRID))
        self.assertEqual([row[3] for row in calls], [Decimal(value) for value in LAMBDA_GRID])
        self.assertEqual(len({row[:3] for row in calls}), 1)
        self.assertEqual(len({row['input_fingerprint'] for row in report['rows']}), 1)
        self.assertEqual(str(CostAwareConfig().cost_weight), '0.15')

    def test_true_control_and_zero_are_compared_not_assumed_identical(self):
        result = evaluate_case(self.case(control=('c', 'b')), k=2)
        self.assertFalse(result['lambda_zero_same_items'])
        self.assertFalse(result['lambda_zero_same_order'])
        self.assertEqual(result['control']['mean_raw_relevance'], 1.5)
        self.assertEqual(result['lambda_zero']['mean_raw_relevance'], 2.5)
        self.assertEqual(result['rows'][0]['comparison']['overlap'], 1)
        aggregate = analyze([self.case(control=('c', 'b'))], k=2)
        self.assertEqual(aggregate['routes']['collaborative_svd']['lambda_zero_item_mismatch_users'], 1)

    def test_order_only_change_is_not_an_item_replacement(self):
        report = evaluate_case(self.case(control=('b', 'a')), k=2)
        comparison = report['rows'][0]['comparison']
        self.assertTrue(report['lambda_zero_same_items'])
        self.assertFalse(report['lambda_zero_same_order'])
        self.assertEqual(comparison['replaced_movies'], 0)
        self.assertEqual(comparison['changed_positions'], 2)
        self.assertEqual(comparison['reordered_common_movies'], 2)
        self.assertEqual(comparison['common_order_inversions'], 1)
        row = analyze([self.case(control=('b', 'a'))], k=2)['routes']['collaborative_svd']['rows'][0]
        self.assertEqual(row['reorder_only_users'], 1)
        self.assertEqual(row['users_with_order_change_rate'], 1)
        self.assertEqual(row['users_with_replacement_rate'], 0)

    def test_exact_cost_difference_overlap_and_relevance_loss(self):
        case = self.case(candidates=[ScoredCandidate('a', 2), ScoredCandidate('b', 1.9), ScoredCandidate('c', 0)],
                         prices={'a': '100', 'b': '0', 'c': '1'}, control=('a',))
        result = evaluate_case(case, k=1)
        row = result['rows'][-1]
        self.assertTrue(row['fulfilled'])
        self.assertEqual(row['comparison']['overlap'], 0)
        self.assertEqual(row['comparison']['replaced_movies'], 1)
        self.assertEqual(row['comparison']['cost_difference'], -100)
        self.assertEqual(row['comparison']['cost_difference_percent'], -100)
        self.assertAlmostEqual(row['comparison']['raw_relevance_loss'], .1)
        self.assertAlmostEqual(row['comparison']['percentile_relevance_loss'], .5)
        self.assertTrue(row['comparison']['lower_predicted_relevance'])
        aggregate = analyze([case], k=1)['routes'][case.route]['rows'][-1]
        self.assertEqual(aggregate['treatment_mean_cost_per_movie'], 0)
        self.assertEqual(aggregate['mean_slate_cost_difference'], -100)
        self.assertEqual(aggregate['replaced_movie_rate'], 1)
        self.assertEqual(aggregate['users_with_replacement_rate'], 1)

    def test_zero_control_cost_has_no_percentage_division(self):
        case = self.case(prices={'a': '0', 'b': '0', 'c': '1'})
        group = analyze([case], k=2)['routes'][case.route]
        for row in group['rows']:
            self.assertIsNone(row['cost_difference_percent'])
            self.assertEqual(row['mean_slate_cost_difference'], 0)
            self.assertEqual(row['no_cost_savings_rate'], 1)

    def test_missing_or_invalid_prices_are_unavailable_not_zero(self):
        for value in (None, '-1', 'NaN', float('inf'), 'malformed'):
            with self.subTest(value=value):
                case = self.case(prices={'a': value, 'b': '1', 'c': '2'})
                result = analyze([case], k=2)
                group = result['routes'][case.route]
                self.assertEqual(group['unknown_candidate_prices'], 1)
                self.assertEqual(group['rows'][0]['cost_paired_users'], 0)
                self.assertIsNone(group['rows'][0]['cost_difference_percent'])
                self.assertEqual(group['rows'][0]['missing_recommended_price_rate'], .5)
                json.dumps(result, allow_nan=False)

    def test_empty_insufficient_and_no_price_cases_count_in_denominator(self):
        good = self.case()
        empty = self.case(candidates=[])
        sparse = self.case(candidates=[ScoredCandidate('a', 1)])
        unknown = self.case(prices={'a': None, 'b': None, 'c': None})
        group = analyze([good, empty, sparse, unknown], k=2)['routes'][good.route]
        for row in group['rows']:
            self.assertEqual(row['attempted_users'], 4)
            self.assertEqual(row['fulfilled_users'], 1)
            self.assertEqual(row['fulfillment_rate'], .25)
            self.assertEqual(row['insufficient_pool_users'], 2)
            self.assertEqual(row['no_known_price_users'], 1)
            self.assertEqual(row['comparison_users'], 1)
            self.assertEqual(row['valid_recommendations'], 2)

    def test_missing_catalog_membership_and_unknown_prices_are_distinct(self):
        case = self.case(prices={'a': '1', 'b': None})
        result = evaluate_case(case, k=2)
        self.assertEqual(result['catalog_membership_failures'], 1)
        self.assertEqual(result['unknown_candidate_prices'], 1)
        self.assertEqual(result['valid_priced_candidates'], 1)
        self.assertTrue(result['rows'][0]['fulfilled'])

    def test_routes_are_separate_and_no_raw_score_pooling_occurs(self):
        cases = [self.case(), self.case(route='llm_content'), self.case(route='popularity_fallback')]
        report = analyze(cases, k=2)
        self.assertEqual(len(report['routes']), 3)
        self.assertNotIn('overall', report)
        for group in report['routes'].values():
            self.assertEqual(group['rows'][0]['attempted_users'], 1)
            self.assertEqual(group['rows'][0]['raw_relevance_users'], 1)

    def test_cost_aggregate_is_equal_user_paired_slate_not_average_percentages(self):
        cheap = self.case(control=('a',), prices=dict(a='1', b='1', c='0'))
        expensive = self.case(control=('a',), prices=dict(a='100', b='0', c='1'))
        row = analyze([cheap, expensive], k=1)['routes'][cheap.route]['rows'][-1]
        self.assertEqual(row['cost_paired_users'], 2)
        self.assertEqual(row['control_mean_slate_cost'], 50.5)
        self.assertEqual(row['treatment_mean_slate_cost'], .5)
        self.assertEqual(row['mean_slate_cost_difference'], -50)
        # Mean individual percentages would be -50%; paired mean cost change is not.
        self.assertAlmostEqual(row['cost_difference_percent'], -10000/101)

    def test_control_outside_pool_has_raw_scores_but_unavailable_percentiles(self):
        case = self.case(control=('outside', 'a'), prices=dict(a=1, b=1, c=1, outside=1), scores={'outside': 4})
        result = evaluate_case(case, k=2)
        self.assertEqual(result['control']['mean_raw_relevance'], 3.5)
        self.assertIsNone(result['control']['mean_relevance_percentile'])
        self.assertIsNone(result['rows'][0]['comparison']['percentile_relevance_loss'])

    def test_json_and_summary_represent_unavailable_metrics_explicitly(self):
        result = analyze([self.case(candidates=[])], k=2)
        decoded = json.loads(json.dumps(result, allow_nan=False))
        self.assertIsNone(decoded['routes']['collaborative_svd']['rows'][0]['treatment_mean_cost_per_movie'])
        self.assertIn('NA', summary_table(result))
        self.assertEqual(len(decoded['lambda_grid']), 9)

    def test_seeded_synthetic_inputs_are_reproducible_diverse_and_database_free(self):
        with patch('src.database.database.get_engine', side_effect=AssertionError('no DB allowed')):
            first = run_sensitivity(dataset='synthetic', sample_size=3, seed=42, k=5)
            second = run_sensitivity(dataset='synthetic', sample_size=3, seed=42, k=5)
            changed = run_sensitivity(dataset='synthetic', sample_size=3, seed=43, k=5)
        self.assertEqual(first, second)
        self.assertNotEqual([row['input_fingerprint'] for row in first['cases']], [row['input_fingerprint'] for row in changed['cases']])
        self.assertEqual(first['dataset']['total_evaluated_users'], 9)
        self.assertEqual(first['candidate_configuration']['requested_pool_size'], 25)
        for route in first['routes'].values():
            self.assertEqual(route['evaluated_users'], 3)
            self.assertGreater(route['distinct_candidate_rankings'], 1)

    def test_cli_exports_json_and_prints_table_without_changing_config(self):
        from contextlib import redirect_stdout
        from io import StringIO
        report = analyze([self.case()], k=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'results.json'
            buffer = StringIO()
            with patch('src.experimentation.cost_sensitivity.run_sensitivity', return_value=report) as run:
                with redirect_stdout(buffer):
                    main(['--dataset', 'synthetic', '--sample-size', '2', '--seed', '4', '--k', '2', '--json', str(path)])
            run.assert_called_once_with(dataset='synthetic', sample_size=2, seed=4, k=2)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertIn('lambda', buffer.getvalue())
        self.assertEqual(CostAwareConfig().cost_weight, Decimal('.15'))

    def test_invalid_dataset_sample_size_k_or_seed_is_rejected(self):
        for kwargs in ({'dataset': 'production'}, {'sample_size': 0}, {'sample_size': 1001},
                       {'sample_size': True}, {'seed': -1}, {'k': 21}):
            with self.assertRaises(ValueError):
                run_sensitivity(**kwargs)


    def test_recorded_data_uses_local_prices_profiles_and_actual_control(self):
        from src.experimentation.cost_sensitivity import ROOT
        artifact = ROOT/'planning/Milestone1/modelTesting/full_models/jace_model.joblib'
        if not artifact.is_file():
            self.skipTest('recorded M0 fixture is not available')
        with patch('src.database.database.get_engine', side_effect=AssertionError('no production database')):
            report = run_sensitivity(dataset='recorded', sample_size=3, seed=42, k=5)
        self.assertEqual(report['dataset']['source'], 'recorded-m0')
        self.assertEqual(report['dataset']['catalog_movies'], 2569)
        self.assertEqual(report['dataset']['total_evaluated_users'], 8)
        self.assertEqual(report['dataset']['eligible_users_per_route'],
                         {'collaborative_svd': 998, 'llm_content': 50, 'popularity_fallback': 2})
        for group in report['routes'].values():
            self.assertEqual(group['lambda_zero_item_mismatch_users'], 0)
            self.assertEqual(group['lambda_zero_order_mismatch_users'], 0)
            self.assertEqual(group['unknown_candidate_prices'], 0)

    def test_replacements_do_not_imply_relative_order_changes(self):
        result = evaluate_case(self.case(control=('b', 'c')), k=2)['rows'][0]['comparison']
        self.assertEqual(result['replaced_movies'], 1)
        self.assertEqual(result['common_order_inversions'], 0)
        self.assertEqual(result['comparable_common_pairs'], 0)
        self.assertIsNone(result['common_order_inversion_rate'])

    def test_cli_json_only_stdout_has_no_table_or_nonfinite_literals(self):
        from contextlib import redirect_stdout
        from io import StringIO
        report = analyze([self.case()], k=2)
        for args in (['--format', 'json'], ['--json', '-']):
            stream = StringIO()
            with patch('src.experimentation.cost_sensitivity.run_sensitivity', return_value=report):
                with redirect_stdout(stream):
                    main(args)
            self.assertEqual(json.loads(stream.getvalue()), report)

    def test_nonfinite_scores_and_large_prices_never_emit_json_nan_or_infinity(self):
        case = self.case(candidates=[ScoredCandidate('a', 1), ScoredCandidate('b', 0), ScoredCandidate('bad', float('inf'))],
                         prices={'a': '1e300', 'b': '1', 'bad': 'NaN'})
        encoded = json.dumps(analyze([case], k=2), allow_nan=False)
        self.assertNotIn('Infinity', encoded)
        self.assertNotIn('NaN', encoded)
        self.assertEqual(json.loads(encoded)['routes'][case.route]['rows'][0]['fulfilled_users'], 1)


    def test_external_case_cannot_exceed_production_candidate_pool_cap(self):
        candidates = [ScoredCandidate(str(index), index) for index in range(101)]
        case = self.case(candidates=candidates, prices={str(i): 1 for i in range(101)})
        with self.assertRaisesRegex(ValueError, 'candidate pool exceeds'):
            evaluate_case(case, k=20)
