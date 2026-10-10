import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from generate_graphs import (
    ALL_MODELS,
    DEFAULT_INFERENCE_RESULTS,
    DEFAULT_MODEL_DIR,
    DEFAULT_TRAINING_RESULTS,
    _latest_level_two_section,
    _parse_results,
    _timing_standard_deviations,
    generate_graphs,
)


class GenerateGraphsTests(unittest.TestCase):
    def test_latest_section_is_selected(self):
        text = "\n".join(
            [
                "## Evaluation old",
                "old data",
                "## Evaluation new",
                "new data",
            ]
        )
        title, section = _latest_level_two_section(text, "## Evaluation ")
        self.assertEqual("## Evaluation new", title)
        self.assertEqual(["## Evaluation new", "new data"], section)

    def test_current_results_parse_expected_models_and_top20(self):
        data = _parse_results(DEFAULT_INFERENCE_RESULTS, DEFAULT_TRAINING_RESULTS)
        self.assertEqual(ALL_MODELS, list(data["pointwise"]))
        self.assertEqual(ALL_MODELS, list(data["native"]))
        self.assertEqual("4893", data["pointwise"]["Nathan"]["Ratings"])
        self.assertEqual("2023", data["throughput_summary"]["Nathan"]["Candidates"])

    def test_timing_standard_deviation(self):
        rows = []
        for model in ALL_MODELS[:4]:
            rows.extend(
                [
                    {"Model": model, "Seconds": "0.001"},
                    {"Model": model, "Seconds": "0.003"},
                ]
            )
        deviations = _timing_standard_deviations(
            rows, "Seconds", multiplier=1000
        )
        self.assertEqual({model: 1.0 for model in ALL_MODELS[:4]}, deviations)

    def test_generates_fourteen_pngs_and_gallery(self):
        with TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            generated = generate_graphs(
                inference_results=DEFAULT_INFERENCE_RESULTS,
                training_results=DEFAULT_TRAINING_RESULTS,
                model_dir=DEFAULT_MODEL_DIR,
                output_dir=output_dir,
            )
            self.assertEqual(14, len(generated))
            for filename in generated:
                path = output_dir / filename
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 1000)
            gallery = (output_dir / "README.md").read_text(encoding="utf-8")
            for filename in generated:
                self.assertIn(f"]({filename})", gallery)


if __name__ == "__main__":
    unittest.main()
