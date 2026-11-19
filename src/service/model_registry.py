"""Verified local model artifacts and request-scoped serving snapshots."""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Mapping
from weakref import WeakSet

import joblib
import numpy as np

from src.service.model_adapter import recommend, validate_model

ACTIVATION_FILE = 'model-activation.json'
ACTIVATION_LOCK = '.model-activation.lock'
LOGGER = logging.getLogger(__name__)


class ActivationError(ValueError):
    """An operator-safe failure code for the readiness response."""


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def version_key(version):
    if (not isinstance(version, str) or not version.strip() or len(version) > 255
            or any(ord(char) < 32 for char in version)):
        raise ValueError('Invalid model version')
    return hashlib.sha256(version.encode()).hexdigest()


def artifact_path(root, sha256):
    if not valid_digest(sha256):
        raise ValueError('Invalid model checksum')
    return root / 'models' / f'{sha256}.joblib'


def registration_path(root, version):
    return root / 'models' / f'{version_key(version)}.json'


def trusted_file(path, root):
    # The mount and its writer are trusted. Reject links and unexpected file types.
    if not path.is_relative_to(root) or path.resolve() != path or not path.is_file():
        raise ValueError('Unexpected model file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError('Unexpected model file')
    return stream


def read_record(path, root):
    with trusted_file(path, root) as stream:
        return json.load(stream)


def model_version(bundle, fallback):
    version = bundle.get('update_metadata', {}).get('version', fallback)
    version_key(version)
    return version


@dataclass(frozen=True, eq=False)
class ModelHandle:
    version: str
    path: Path | None
    sha256: str | None
    bundle: dict
    provenance: Mapping


def load_model(root, path, fallback_version, expected_sha256=None, provenance=None):
    with trusted_file(path, root) as stream:
        sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise ActivationError('checksum_mismatch')
        stream.seek(0)
        bundle = joblib.load(stream)
    validate_model(bundle)
    version = model_version(bundle, fallback_version)
    user_factors, movie_factors = bundle['user_factors'], bundle['movie_factors']
    if (user_factors.ndim != 2 or movie_factors.ndim != 2
            or user_factors.shape != (len(bundle['ui']), movie_factors.shape[0])
            or bundle['popularity'].shape != (len(bundle['movies']),)):
        raise ValueError('Incompatible model score shapes')
    for name in ('user_factors', 'movie_factors', 'popularity'):
        if not np.isfinite(bundle[name]).all():
            raise ValueError('Nonfinite model scores')
    # Exercise both an unrecognized user and the trained scoring path.
    users = (next(iter(bundle['ui']), ''), '')
    for user in users:
        ids, _ = recommend(bundle, user)
        if (not user and not ids) or any(not item or any(char in item for char in ',\r\n') for item in ids):
            raise ValueError('Model warmup produced invalid recommendations')
    query = bundle['vectorizer'].transform(['warmup'])
    if (bundle['content'] @ query.T).toarray().shape != (len(bundle['movies']), 1):
        raise ValueError('Incompatible content features')
    return ModelHandle(version, path, sha256, bundle,
                       MappingProxyType(dict(provenance or {}) | bundle.get('update_metadata', {})))


@dataclass(frozen=True)
class ServingSnapshot:
    default: ModelHandle | None
    pins: Mapping[str, ModelHandle | None]
    previous: ModelHandle | None = None
    activation_id: str | None = None
    activation_status: str = 'idle'
    activation_error: str | None = None


class ModelRegistry:
    """One instance per serving worker. Bundles are treated as read-only."""

    def __init__(self, bootstrap_path, fallback_version, *, model=None, pins=None, capacity=5):
        configured_path = Path(bootstrap_path)
        self.root = configured_path.parent.resolve()
        self.bootstrap = self.root / configured_path.name
        self.manifest = self.root / ACTIVATION_FILE
        self.capacity = capacity
        self._publication = Lock()
        self._staging = Lock()
        self._handles = WeakSet()
        self._rejected = None
        self._applied = None
        default = None
        if model is not None:
            validate_model(model)
            default = ModelHandle(model_version(model, fallback_version), None, None, model,
                                  MappingProxyType(dict(model.get('update_metadata', {}))))
        elif self.bootstrap.is_file():
            default = load_model(self.root, self.bootstrap, fallback_version)
            sidecar = self.bootstrap.with_suffix('.json')
            if sidecar.exists():
                record = read_record(sidecar, self.root)
                if not default.bundle.get('update_metadata') and record.get('model_sha256') != default.sha256:
                    raise ValueError('Legacy model checksum does not match its sidecar')
                if record.get('model_sha256') == default.sha256:
                    default = ModelHandle(default.version, default.path, default.sha256, default.bundle,
                                          MappingProxyType(record | dict(default.provenance)))
            binding = registration_path(self.root, default.version)
            if binding.exists():
                record = read_record(binding, self.root)
                if record['version'] != default.version or record['sha256'] != default.sha256:
                    raise ValueError('Bootstrap model version already bound to different bytes')
                default = ModelHandle(default.version, default.path, default.sha256, default.bundle,
                                      MappingProxyType(record['provenance'] | dict(default.provenance)))
        if default is not None:
            self._track(default)
        pinned = {}
        for arm, version in (pins or {}).items():
            if not version:
                continue
            try:
                pinned[arm] = self._registered(version)
            except Exception as error:
                LOGGER.warning('model_pin_unavailable arm=%s error_type=%s', arm, type(error).__name__)
                pinned[arm] = None
        self._snapshot = ServingSnapshot(default, MappingProxyType(pinned))

    def snapshot(self):
        with self._publication:
            return self._snapshot

    def _track(self, handle):
        if len(self._handles) >= self.capacity:
            raise ActivationError('capacity_reached')
        self._handles.add(handle)
        return handle

    def _registered(self, version, expected_sha256=None):
        record = read_record(registration_path(self.root, version), self.root)
        sha256 = record['sha256']
        if record['version'] != version or (expected_sha256 is not None and sha256 != expected_sha256):
            raise ActivationError('registration_mismatch')
        for handle in list(self._handles):
            if handle.version == version:
                if handle.sha256 != sha256:
                    raise ActivationError('version_reused')
                return handle
        if len(self._handles) >= self.capacity:
            raise ActivationError('capacity_reached')
        handle = load_model(self.root, artifact_path(self.root, sha256), version, sha256, record['provenance'])
        if handle.version != version:
            raise ActivationError('embedded_version_mismatch')
        return self._track(handle)

    def refresh(self):
        """Apply one trusted instruction. Concurrent refresh attempts do not queue."""
        if not self._staging.acquire(blocking=False):
            return
        try:
            self._refresh()
        finally:
            self._staging.release()

    def _refresh(self):
        current = self.snapshot()
        instruction = None
        activation_id = None
        try:
            if not self.manifest.exists():
                return
            instruction = read_record(self.manifest, self.root)
            if instruction == self._rejected:
                return
            activation_id = instruction['activation_id']
            if not isinstance(activation_id, str) or not re.fullmatch('[a-f0-9]{32}', activation_id):
                activation_id = None
                raise ValueError('Invalid activation ID')
            if self._applied is not None and self._applied['activation_id'] == activation_id:
                if self._applied != instruction:
                    raise ActivationError('activation_id_reused')
                if current.activation_status == 'loaded':
                    return
            base = instruction['base_sha256']
            if not valid_digest(base) or not valid_digest(instruction['sha256']):
                raise ValueError('Invalid activation checksum')
            if current.default is None or current.default.sha256 not in (base, instruction['sha256']):
                raise ActivationError('stale_base')
            handle = self._registered(instruction['version'], instruction['sha256'])
            previous = current.previous if handle is current.default else current.default
            updated = ServingSnapshot(handle, current.pins, previous, activation_id, 'loaded')
            # Serialize the final reread + publication with the trusted writer.
            # Neither artifact loading nor waiting for the writer holds the request lock.
            with trusted_file(self.root / ACTIVATION_LOCK, self.root) as stream:
                fcntl.flock(stream, fcntl.LOCK_SH)
                if read_record(self.manifest, self.root) != instruction:
                    return
                with self._publication:
                    self._snapshot = updated
            self._rejected = None
            self._applied = instruction
        except Exception as error:
            self._rejected = instruction
            code = str(error) if isinstance(error, ActivationError) else 'invalid_model_or_instruction'
            if current.activation_id != activation_id or current.activation_status != 'failed':
                LOGGER.warning('model_activation_failed code=%s error_type=%s', code, type(error).__name__)
            updated = ServingSnapshot(current.default, current.pins, current.previous,
                                      activation_id, 'failed', code)
            with self._publication:
                self._snapshot = updated

    def watch(self):
        """Start the watcher after the serving worker has been forked."""
        self.refresh()

        def poll():
            timer = Event()
            while not timer.wait(0.5):
                self.refresh()

        Thread(target=poll, name='model-activation', daemon=True).start()
