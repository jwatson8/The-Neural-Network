"""Capture structured messages without network, service, or database imports."""

import json
import logging
import unittest
from unittest.mock import patch

from src.data_quality.boundary import validate_stream_event, validate_user_response
from src.data_quality.drift import detect_missing_value_drift, detect_rating_drift
from src.data_quality.quality_logging import log_drift_result, log_validation_failure


class Unprintable:
    def __str__(self):
        raise AssertionError("Sensitive objects must not be stringified")

    __repr__ = __str__


class QualityLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        sink_patch = patch("src.data_quality.quality_logging.POSTGRES_SINK.write")
        self.addCleanup(sink_patch.stop)
        sink_patch.start()
        self.logger = logging.getLogger(f"quality-test.{self.id()}")
        self.errors = [{"loc": ("rating",), "type": "less_than_equal", "msg": "unused"}]

    def capture(self, function, *args, level=logging.INFO, **kwargs) -> tuple[dict, logging.LogRecord]:
        with self.assertLogs(self.logger, level=level) as captured:
            function(*args, logger=self.logger, **kwargs)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        payload = json.loads(record.getMessage())
        json.dumps(payload, allow_nan=False)
        return payload, record

    def test_validation_failure_for_all_boundaries(self) -> None:
        for source in ("kafka", "user_api", "movie_api", "recommendation_api"):
            with self.subTest(source=source):
                payload, record = self.capture(log_validation_failure, source, self.errors)
                self.assertEqual(record.levelno, logging.WARNING)
                self.assertEqual(payload["source"], source)
                self.assertEqual(payload["issue_type"], "validation_failure")
                self.assertEqual(payload["validation_failure_count"], 1)
                self.assertEqual(payload["rejected_record_count"], 1)
                self.assertEqual(payload["errors"][0], {
                    "field": "rating", "location": ["rating"],
                    "validation_error_type": "less_than_equal",
                })

    def test_multiple_errors_are_not_multiple_rejected_records(self) -> None:
        errors = self.errors + [{"loc": ("user_id",), "type": "missing"}]
        payload, _ = self.capture(log_validation_failure, "kafka", errors)
        self.assertEqual(payload["validation_failure_count"], 2)
        self.assertEqual(payload["rejected_record_count"], 1)
        batch, _ = self.capture(log_validation_failure, "kafka", errors, rejected_record_count=2)
        self.assertEqual(batch["rejected_record_count"], 2)

    def test_field_locations_and_root_errors(self) -> None:
        errors = [
            {"loc": ("user", "self_description_likes"), "type": "string_type"},
            {"loc": ("recommendations", 2, "movie_id"), "type": "value_error"},
            {"loc": (), "type": "value_error"},
        ]
        payload, _ = self.capture(log_validation_failure, "recommendation_api", errors)
        self.assertEqual([item["field"] for item in payload["errors"]],
                         ["user.self_description_likes", "recommendations.<index>.movie_id", "$"])

    def test_actual_validator_errors(self) -> None:
        for validator, raw in ((validate_stream_event, {}),
                               (validate_user_response, {"user": {"user_id": None}})):
            _, errors = validator(raw)
            payload, _ = self.capture(log_validation_failure, "kafka", errors)
            self.assertEqual(payload["validation_failure_count"], len(errors))

    def test_sensitive_fields_values_messages_and_context_are_omitted(self) -> None:
        secret = "private-user-credential-preferences"
        errors = [{"loc": (secret, Unprintable()), "type": secret,
                   "msg": secret, "input": secret, "ctx": {"error": Unprintable()},
                   "url": secret}]
        payload, record = self.capture(log_validation_failure, "user_api", errors)
        self.assertNotIn(secret, record.getMessage())
        self.assertEqual(payload["errors"][0]["location"], ["<redacted>", "<redacted>"])
        self.assertEqual(payload["errors"][0]["validation_error_type"], "<redacted>")

    def test_validation_inputs_are_not_changed(self) -> None:
        original = [{"loc": ("rating",), "type": "less_than_equal", "msg": "unused"}]
        self.capture(log_validation_failure, "kafka", self.errors)
        self.assertEqual(self.errors, original)

    def test_empty_errors_do_not_log(self) -> None:
        with self.assertNoLogs(self.logger):
            log_validation_failure("kafka", [], logger=self.logger)

    def test_drift_detected(self) -> None:
        result = detect_rating_drift([{"rating": 1}] * 100, [{"rating": 10}] * 100)
        payload, record = self.capture(log_drift_result, result)
        self.assertEqual(record.levelno, logging.WARNING)
        for key in ("metric", "baseline_size", "recent_size", "drift_score", "threshold", "status", "min_samples"):
            self.assertEqual(payload[key], result[key])
        self.assertEqual(payload["source"], "drift")
        self.assertEqual(payload["issue_type"], "drift_result")

    def test_no_drift(self) -> None:
        rows = [{"rating": 5}] * 100
        payload, record = self.capture(log_drift_result, detect_rating_drift(rows, rows))
        self.assertEqual(record.levelno, logging.INFO)
        self.assertEqual(payload["status"], "no_drift")
        self.assertEqual(payload["drift_score"], 0.0)

    def test_insufficient_data(self) -> None:
        payload, record = self.capture(log_drift_result, detect_rating_drift([], []))
        self.assertEqual(record.levelno, logging.INFO)
        self.assertEqual(payload["status"], "insufficient_data")
        self.assertIsNone(payload["drift_score"])

    def test_invalid_drift_data(self) -> None:
        result = detect_rating_drift([{"rating": float("nan")}], [{"rating": 1}])
        payload, record = self.capture(log_drift_result, result)
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertEqual(payload["status"], "invalid_data")
        self.assertIsNone(payload["drift_score"])
        self.assertEqual(payload["errors"][0]["field"], "baseline.<index>.rating")

    def test_drift_payloads_and_custom_field_names_do_not_leak(self) -> None:
        secret = "private-profile-preferences"
        result = detect_missing_value_drift([{}], [{secret: 1}], [secret], min_samples=1)[0]
        result["raw_profile"] = secret
        result["errors"] = [{"loc": (secret,), "type": "value_error", "msg": secret,
                              "input": Unprintable(), "ctx": Unprintable()}]
        payload, record = self.capture(log_drift_result, result)
        self.assertNotIn(secret, record.getMessage())
        self.assertEqual(payload["metric"], "missing_value_rate:<redacted>")
        self.assertEqual(result["raw_profile"], secret)

    def test_missingness_metric_field_is_retained_when_known(self) -> None:
        result = detect_missing_value_drift([{}], [{}], ["movie_id"], min_samples=1)[0]
        payload, _ = self.capture(log_drift_result, result)
        self.assertEqual(payload["metric"], "missing_value_rate:movie_id")

    def test_no_handlers_or_global_configuration_added(self) -> None:
        root = logging.getLogger()
        root_handlers, handlers = list(root.handlers), list(self.logger.handlers)
        root_level, level = root.level, self.logger.level
        with patch.object(self.logger, "warning") as warning:
            log_validation_failure("kafka", self.errors, logger=self.logger)
            log_validation_failure("kafka", self.errors, logger=self.logger)
            self.assertEqual(warning.call_count, 2)
        self.assertEqual(root.handlers, root_handlers)
        self.assertEqual(self.logger.handlers, handlers)
        self.assertEqual((root.level, self.logger.level), (root_level, level))

    def test_default_module_logger(self) -> None:
        with self.assertLogs("src.data_quality.quality_logging", level="WARNING") as captured:
            log_validation_failure("movie_api", self.errors)
        self.assertEqual(len(captured.records), 1)

    def test_invalid_validation_inputs_raise(self) -> None:
        for source in ("private-user", None, []):
            with self.subTest(source=source), self.assertRaises(ValueError):
                log_validation_failure(source, self.errors)
        for errors in (None, {}, "errors", [None], [{"loc": "rating", "type": "value_error"}]):
            with self.subTest(errors=errors), self.assertRaises(TypeError):
                log_validation_failure("kafka", errors)
        with self.assertRaises(KeyError):
            log_validation_failure("kafka", [{}])
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                log_validation_failure("kafka", self.errors, rejected_record_count=count)

    def test_invalid_logger_and_unexpected_errors_propagate(self) -> None:
        with self.assertRaises(TypeError):
            log_validation_failure("kafka", self.errors, logger="logger")
        with self.assertRaises(TypeError):
            log_drift_result({}, logger=object())
        with patch.object(self.logger, "warning", side_effect=RuntimeError("broken logger")):
            with self.assertRaises(RuntimeError):
                log_validation_failure("kafka", self.errors, logger=self.logger)

    def test_invalid_drift_inputs_raise_without_logging(self) -> None:
        result = detect_rating_drift([{"rating": 1}] * 100, [{"rating": 10}] * 100)
        for key, value in (("threshold", float("nan")), ("drift_score", float("inf")),
                           ("baseline_size", -1), ("recent_size", True),
                           ("status", "unknown"), ("metric", "unknown"),
                           ("min_samples", 0), ("drift_score", None)):
            with self.subTest(key=key), self.assertNoLogs(self.logger):
                with self.assertRaises(ValueError):
                    log_drift_result({**result, key: value}, logger=self.logger)
        with self.assertRaises(TypeError):
            log_drift_result([])
        with self.assertRaises(KeyError):
            log_drift_result({})
        with self.assertRaises(ValueError):
            log_drift_result({**result, "status": "insufficient_data"})

    def test_non_string_drift_metric_is_rejected_without_serialization(self) -> None:
        result = detect_rating_drift([], [])
        for metric in (None, 42, {}, Unprintable()):
            with self.assertNoLogs(self.logger):
                with self.assertRaises(TypeError):
                    log_drift_result({**result, "metric": metric}, logger=self.logger)


if __name__ == "__main__":
    unittest.main()
