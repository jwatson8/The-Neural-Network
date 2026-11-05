"""Exercise deployment control flow without SSH, Docker or production access."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'deployment/deploy_vm.sh'
SHA = 'a' * 40


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Linux deployment script')
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'artifacts').mkdir()
        (self.root / 'artifacts/model.joblib').write_text('fixture')
        (self.root / '.env').write_text('fixture')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        git = self.bin / 'git'
        git.write_text('''#!/usr/bin/env bash
set -eu
shift 2
case "$1" in
  status) printf '%s' "${FAKE_DIRTY:-}" ;;
  fetch) : ;;
  rev-parse) if [[ "$2" == origin/main ]]; then echo "${FAKE_MAIN}"; else printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n'; fi ;;
  checkout) echo "$*" >> "$DEPLOY_DIRECTORY/commands" ;;
  *) exit 99 ;;
esac
''')
        docker = self.bin / 'docker'
        docker.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DEPLOY_DIRECTORY/commands"
if [[ "${FAIL_START:-}" == yes && "$*" == *"up -d"* ]]; then exit 1; fi
if [[ "${FAIL_CONFIG:-}" == yes && "$*" == *"run --rm --no-deps"* ]]; then exit 1; fi
''')
        git.chmod(0o755)
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        DEPLOY_DIRECTORY=str(self.root), FAKE_MAIN=SHA)

    def run_deploy(self, sha=SHA, live='true'):
        return subprocess.run(['bash', str(SCRIPT), sha, live], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_success_uses_tested_commit_and_preserves_inputs(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = (self.root / 'commands').read_text()
        self.assertIn('checkout --detach ' + SHA, commands)
        self.assertIn('--profile live up -d --wait', commands)
        self.assertNotIn('down', commands)
        self.assertEqual((self.root / 'state/deployed-commit.txt').read_text().strip(), SHA)
        self.assertEqual((self.root / '.env').read_text(), 'fixture')
        self.assertEqual((self.root / 'artifacts/model.joblib').read_text(), 'fixture')

    def test_stale_commit_and_dirty_checkout_do_not_deploy(self):
        for change in ({'FAKE_MAIN': 'c'*40}, {'FAKE_DIRTY': ' M src/service/api.py'}):
            with self.subTest(change=change):
                previous = self.env.copy()
                self.env.update(change)
                self.assertNotEqual(self.run_deploy().returncode, 0)
                self.assertFalse((self.root / 'commands').exists())
                self.env = previous

    def test_missing_model_and_invalid_sha_are_rejected(self):
        self.assertNotEqual(self.run_deploy(sha='main; echo unsafe').returncode, 0)
        (self.root / 'artifacts/model.joblib').unlink()
        self.assertNotEqual(self.run_deploy().returncode, 0)
        self.assertFalse((self.root / 'commands').exists())

    def test_failed_start_does_not_record_success(self):
        self.env['FAIL_START'] = 'yes'
        self.assertNotEqual(self.run_deploy().returncode, 0)
        self.assertFalse((self.root / 'state/deployed-commit.txt').exists())

    def test_api_only_does_not_start_live_profile(self):
        result = self.run_deploy(live='false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--profile live', (self.root / 'commands').read_text())

    def test_missing_live_configuration_does_not_restart_production(self):
        self.env['FAIL_CONFIG'] = 'yes'
        self.assertNotEqual(self.run_deploy().returncode, 0)
        self.assertNotIn('up -d', (self.root / 'commands').read_text())
