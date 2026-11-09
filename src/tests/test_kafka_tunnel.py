"""Validate the Compose-managed SSH tunnel without making network connections."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "deployment" / "kafka_tunnel.sh"


class KafkaTunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.known_hosts = self.root / "known_hosts"
        self.environment = {
            **os.environ,
            "KAFKA_SSH_HOST": "128.2.24.239",
            "KAFKA_SSH_USER": "kafkatunnel",
            "KAFKA_SSH_PORT": "22",
            "KAFKA_REMOTE_HOST": "localhost",
            "KAFKA_REMOTE_PORT": "9092",
            "KAFKA_TUNNEL_LOCAL_PORT": "9092",
            "KAFKA_TOPIC": "movielog1",
            "KAFKA_GROUP_ID": "test-group",
            "KAFKA_SSH_KNOWN_HOSTS": "128.2.24.239 ssh-ed25519 verified-key",
            "KAFKA_SSH_KNOWN_HOSTS_FILE": str(self.known_hosts),
            "SSHPASS": "ssh-password",
        }

    def run_script(self, *arguments, environment=None):
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *arguments],
            env=environment or self.environment,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_check_accepts_complete_configuration_without_connecting(self):
        result = self.run_script("--check")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_rejects_missing_configuration(self):
        environment = self.environment.copy()
        environment["KAFKA_TOPIC"] = ""
        result = self.run_script("--check", environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("KAFKA_TOPIC", result.stderr)

        environment["KAFKA_TOPIC"] = "movielog1"
        environment["SSHPASS"] = ""
        result = self.run_script("--check", environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSHPASS", result.stderr)

    def test_foreground_command_uses_verified_password_tunnel(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_sshpass = fake_bin / "sshpass"
        fake_sshpass.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
            encoding="utf-8",
        )
        fake_sshpass.chmod(0o755)
        environment = self.environment.copy()
        environment["PATH"] = str(fake_bin) + os.pathsep + os.defpath

        result = self.run_script(environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.splitlines()
        self.assertEqual(arguments[:3], ["-e", "ssh", "-NT"])
        self.assertIn("UserKnownHostsFile=" + str(self.known_hosts), arguments)
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertIn("ExitOnForwardFailure=yes", arguments)
        self.assertIn("127.0.0.1:9092:localhost:9092", arguments)
        self.assertEqual(arguments[-1], "kafkatunnel@128.2.24.239")
        self.assertEqual(
            self.known_hosts.read_text(encoding="utf-8"),
            "128.2.24.239 ssh-ed25519 verified-key\n",
        )


if __name__ == "__main__":
    unittest.main()
