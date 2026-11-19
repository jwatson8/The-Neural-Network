"""Exercise the operator script's activation and rollback control flow offline."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'deployment/daily_model.sh'
CANDIDATE = 'c' * 64
CHAMPION = 'a' * 64


@pytest.mark.parametrize('failure', ['', 'load', 'smoke', 'rollback'])
def test_wrapper_finishes_only_verified_activation(tmp_path, failure):
    deployment = tmp_path / 'deployment'
    deployment.mkdir()
    shutil.copyfile(SCRIPT, deployment / SCRIPT.name)
    (tmp_path / 'artifacts').mkdir()
    lock = tmp_path / 'flock'
    lock.write_text('#!/bin/sh\nexit 0\n')
    lock.chmod(0o755)
    docker = tmp_path / 'docker'
    docker.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import sys

args = sys.argv[1:]
failure = os.environ['FAKE_PROMOTION_FAILURE']
with Path('calls').open('a') as stream:
    stream.write(' '.join(args[:8]).split('python -c')[0] + '\\n')
if args == ['compose', '-p', 'the-neural-network', 'ps', '-q', 'api']:
    print('isolated-api')
elif args[0] == 'inspect':
    print('isolated-image')
elif 'exec' in args:
    if args[-2:] == ['candidate', 'c' * 64]:
        if failure:
            sys.exit('Failed loading' if failure == 'load' else 'Failed smoke verification')
    elif args[-2:] == ['rollback', 'a' * 64]:
        if failure == 'rollback':
            sys.exit('Rollback failed')
    elif 'activation_enabled' in args[-1]:
        print('baseline-v1')
    else:
        sys.exit('Unexpected verification command')
elif 'src.model_updates.promotion' in args:
    command = args[args.index('src.model_updates.promotion') + 1:]
    with Path('transactions').open('a') as stream:
        stream.write(' '.join(command) + '\\n')
    if command[0] == 'stage':
        print('candidate', 'c' * 64)
    elif command[0] == 'rollback':
        print('rollback', 'a' * 64)
else:
    sys.exit('Unexpected Docker command')
''')
    docker.chmod(0o755)
    result = subprocess.run(
        ['bash', str(deployment / SCRIPT.name), 'promote', '2026-01-03'],
        env=os.environ | {'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
                          'FAKE_PROMOTION_FAILURE': failure},
        capture_output=True, text=True, timeout=10,
    )
    actions = (tmp_path / 'transactions').read_text().splitlines()
    assert actions[0] == 'stage 2026-01-03 --bootstrap-version baseline-v1'
    assert 'restart' not in (tmp_path / 'calls').read_text()
    if not failure:
        assert result.returncode == 0, result.stderr
        assert actions[1:] == [f'finish 2026-01-03 candidate {CANDIDATE}']
    else:
        assert result.returncode != 0
        assert actions[1] == 'rollback 2026-01-03'
        if failure == 'rollback':
            assert len(actions) == 2
            assert 'remains pending' in result.stderr
        else:
            assert actions[2:] == [f'finish-rollback 2026-01-03 rollback {CHAMPION}']
