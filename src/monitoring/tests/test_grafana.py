"""Tests for Grafana database access and provisioned assets."""

import json
import os
import re
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

    def test_reader_role_commits_and_closes_resources(self):
        cursor = Mock()
        cursor.fetchone.return_value = None
        connection = Mock()
        connection.cursor.return_value = cursor
        engine = Mock()
        engine.url = SimpleNamespace(database="recommender")
        engine.raw_connection.return_value = connection

        with patch.object(provision_reader, "get_engine", return_value=engine):
            provision_reader.provision_reader("reader-secret")

        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_reader_role_rolls_back_and_closes_resources_on_failure(self):
        cursor = Mock()
        cursor.execute.side_effect = RuntimeError("database unavailable")
        connection = Mock()
        connection.cursor.return_value = cursor
        engine = Mock()
        engine.url = SimpleNamespace(database="recommender")
        engine.raw_connection.return_value = connection

        with patch.object(
            provision_reader, "get_engine", return_value=engine
        ), self.assertRaisesRegex(RuntimeError, "database unavailable"):
            provision_reader.provision_reader("reader-secret")

        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_dashboard_queries_use_provisioned_datasources_and_readable_tables(self):
        datasource_root = GRAFANA_ROOT / "provisioning" / "datasources"
        monitoring_config = (datasource_root / "monitoring.yaml").read_text()
        data_view_config = (datasource_root / "data-view.yaml").read_text()
        provisioned_uids = {
            match.group(1)
            for config in (monitoring_config, data_view_config)
            for match in re.finditer(r"^\s*uid:\s*(\S+)\s*$", config, re.MULTILINE)
        }
        allowed_hosts = {
            match.group(1)
            for match in re.finditer(
                r"^\s*-\s*(https?://\S+)\s*$", data_view_config, re.MULTILINE
            )
        }

        dashboard_paths = list((GRAFANA_ROOT / "dashboards").glob("*.json"))
        self.assertTrue(dashboard_paths)
        for dashboard_path in dashboard_paths:
            dashboard = json.loads(dashboard_path.read_text())
            panel_ids = [panel["id"] for panel in dashboard["panels"]]
            self.assertEqual(len(panel_ids), len(set(panel_ids)))
            for panel in dashboard["panels"]:
                panel_datasource = panel["datasource"]["uid"]
                self.assertIn(panel_datasource, provisioned_uids)
                for target in panel["targets"]:
                    self.assertEqual(target["datasource"]["uid"], panel_datasource)
                    raw_sql = target.get("rawSql")
                    url = target.get("url")
                    self.assertTrue(raw_sql or url)
                    if raw_sql:
                        cte_names = {
                            name.lower()
                            for name in re.findall(
                                r"(?:WITH|,)\s+(\w+)\s+AS\s*\(",
                                raw_sql,
                                re.IGNORECASE,
                            )
                        }
                        referenced_tables = {
                            name.lower()
                            for name in re.findall(
                                r"\b(?:FROM|JOIN)\s+(?:public\.)?(\w+)",
                                raw_sql,
                                re.IGNORECASE,
                            )
                        } - cte_names
                        self.assertLessEqual(
                            referenced_tables, set(provision_reader.READABLE_TABLES)
                        )
                    if url:
                        self.assertTrue(
                            any(url.startswith(host) for host in allowed_hosts)
                        )


if __name__ == "__main__":
    unittest.main()
