"""Immutable training-code identity and retained training input manifests."""
import hashlib
import os
from pathlib import Path
import platform
import re
import subprocess

import numpy
import scipy
import sklearn


def training_code():
    """Containers use a baked revision, never a deployment-time environment guess."""
    root = Path(__file__).resolve().parents[2]
    revision = root / 'build-revision.txt'
    if revision.exists():
        sha = revision.read_text().strip()
    else:
        try:
            sha = subprocess.check_output(
                ['git', '-c', f'safe.directory={root}', 'rev-parse', 'HEAD'],
                cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            dirty = subprocess.check_output(
                ['git', '-c', f'safe.directory={root}', 'status', '--porcelain', '--untracked-files=no'],
                cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            if dirty:
                sha = 'uncommitted'
        except (OSError, subprocess.SubprocessError):
            sha = 'unknown'
    sources = sorted((root / 'src').rglob('*.py'))
    digest = hashlib.sha256()
    for path in sources:
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return dict(git_commit_sha=sha if re.fullmatch('[a-f0-9]{40}', sha) else None,
                training_source_sha256=digest.hexdigest(),
                libraries=dict(python=platform.python_version(), numpy=numpy.__version__,
                               scipy=scipy.__version__, scikit_learn=sklearn.__version__))


def retain_original_data(data_dir, root):
    """Keep content-addressed copies, so later image rebuilds cannot lose inputs."""
    import shutil
    result = {}
    for source in sorted(Path(data_dir).glob('*.csv.gz')):
        with source.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        target = Path(root) / 'training-data' / sha / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix('.copying')
            shutil.copyfile(source, temporary)
            temporary.replace(target)
        with target.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                raise ValueError('Retained training data checksum mismatch')
        result[source.name] = dict(sha256=sha, path=target.relative_to(root).as_posix())
    return result
