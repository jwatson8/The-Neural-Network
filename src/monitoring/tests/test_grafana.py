"""Tests for Grafana database access and provisioned assets."""

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.monitoring.grafana import provision_reader

GRAFANA_ROOT = Path(__file__).parents[1] / "grafana"


class GrafanaProvisioningTests(unittest.TestCase):
    def test_required_secrets_reject_missing_or_blank_values(self):
        for value in (None, "", "   "):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["GRAFANA_DB_PASSWORD"] = value
                with self.assertRaisesRegex(RuntimeError, "GRAFANA_DB_PASSWORD"):
                    provision_reader._required_secret("GRAFANA_DB_PASSWORD")

    def test_reader_role_is_committed_and_connections_are_closed(self):
        cursor = Mock()
        cursor.fetchone.return_value = None
        connection = Mock()
        connection.cursor.return_value = cursor
        engine = Mock()
        engine.url = SimpleNamespace(database="recommender")
        engine.raw_connection.return_value = connection

        with patch.object(provision_reader, "get_engine", return_value=engine):
            provision_reader.provision_reader("reader-secret")

        self.assertGreaterEqual(cursor.execute.call_count, 10)
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_dashboard_and_provisioning_assets_have_stable_contracts(self):
        dashboard_path = GRAFANA_ROOT / "dashboards" / "monitoring-overview.json"
        dashboard = json.loads(dashboard_path.read_text())
        panel_titles = {panel["title"] for panel in dashboard["panels"]}

        self.assertEqual(dashboard["uid"], "monitoring-overview")
        self.assertEqual(len(dashboard["panels"]), 8)
        self.assertIn("Successful Run Freshness", panel_titles)
        self.assertIn("Monitoring Failures", panel_titles)
        self.assertIn("Application Table Row Counts", panel_titles)
        summary_panel = next(
            panel for panel in dashboard["panels"]
            if panel["title"] == "Application Table Row Counts"
        )
        summary_query = summary_panel["targets"][0]
        self.assertEqual(summary_query["url"], "http://api:8082/database/summary")
        self.assertEqual(summary_query["url_options"]["method"], "GET")
        self.assertEqual(summary_query["uql"], 'parse-json | project kv("tables")')
        queries = " ".join(
            target["rawSql"]
            for panel in dashboard["panels"]
            for target in panel["targets"]
            if "rawSql" in target
        )
        self.assertIn("INTERVAL '5 minutes'", queries)
        self.assertIn("Rolling 24-hour accuracy", queries)
        self.assertNotIn("$__timeFrom() - INTERVAL", queries)
        self.assertIn("CAST($__timeFrom() AS timestamptz)", queries)

        datasource = (
            GRAFANA_ROOT / "provisioning" / "datasources" / "monitoring.yaml"
        ).read_text()
        provider = (
            GRAFANA_ROOT / "provisioning" / "dashboards" / "monitoring.yaml"
        ).read_text()
        self.assertIn("uid: monitoring-postgres", datasource)
        self.assertIn("password: $__env{GRAFANA_DB_PASSWORD}", datasource)
        self.assertIn("path: /etc/grafana/dashboards", provider)
        data_view = (
            GRAFANA_ROOT / "provisioning" / "datasources" / "data-view.yaml"
        ).read_text()
        self.assertIn("uid: data-view-api", data_view)
        self.assertIn("http://api:8082", data_view)


if __name__ == "__main__":
    unittest.main()
