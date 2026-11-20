"""Validate the Compose migration graph without starting Docker or contacting a DB."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is needed to parse its config")
class DatabaseComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=15)
        if version.returncode:
            raise unittest.SkipTest("Docker Compose plugin is unavailable")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env") as env_file:
            result = subprocess.run(["docker", "compose", "--env-file", env_file.name,
                "--file", str(ROOT / "compose.yaml"), "--profile", "live", "--profile", "monitoring",
                "--profile", "bootstrap",
                "config", "--format", "json"], env=dict(os.environ, POSTGRES_PASSWORD="compose-test-only"),
                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.services = json.loads(result.stdout)["services"]

    def test_dedicated_migration_job_precedes_initialization(self):
        migration = self.services["database-migrate"]
        self.assertEqual(migration["restart"], "no")
        self.assertEqual(migration["command"], ["python", "-m",
            "src.database.migrations.apply_all", "--apply", "--bootstrap-if-empty"])
        self.assertEqual(migration["depends_on"]["database"]["condition"], "service_healthy")
        self.assertEqual(self.services["database-init"]["depends_on"]["database-migrate"]["condition"],
                         "service_completed_successfully")

    def test_api_and_database_workers_are_gated_by_migration_success(self):
        graph = {"database-roll-up": "database-init", "api": "database-init",
                 "daily-model-updater": "database-init", "monitoring-worker": "database-init",
                 "grafana-database-init": "database-init"}
        for service, dependency in graph.items():
            with self.subTest(service=service):
                self.assertEqual(self.services[service]["depends_on"][dependency]["condition"],
                                 "service_completed_successfully")
        self.assertEqual(self.services["database-init"]["command"], ["python", "-m", "src.database.initialize"])

    def test_fixed_data_roll_up_is_an_explicit_bootstrap_operation(self):
        self.assertEqual(self.services["database-roll-up"]["profiles"], ["bootstrap"])
        self.assertNotIn("database-roll-up", self.services["api"]["depends_on"])
