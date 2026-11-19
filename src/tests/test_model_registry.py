"""Exercise persisted artifacts and the single-worker activation protocol offline."""

from datetime import date
from threading import Event, Thread
from unittest.mock import patch

import joblib
import pytest

from src.model.recommender import fit
from src.model_updates.daily import digest, write_json
from src.model_updates.promotion import activation, finish, publish_activation, register, request_rollback, stage
from src.service.model_registry import ACTIVATION_FILE, ModelRegistry, artifact_path


def write_bundle(path, version, seen=0):
    users = [{'user_id': user} for user in ('warm', 'other')]
    movies = [{'movie_id': f'movie-{i}', 'title': f'Adventure number {i}'} for i in range(22)]
    events = [dict(timestamp='2026-01-01', user_id=user, movie_id=f'movie-{movie}',
                   event_type='rating', rating=9)
              for user, movie in [('warm', seen), ('other', (seen + 1) % len(movies))]]
    model = fit(events, users, movies)
    model['update_metadata'] = dict(version=version, training_end_exclusive='2026-01-02T00:00:00Z')
    joblib.dump(model, path)
    return digest(path)


def install(root, version, seen=0):
    source = root / f'{version}.joblib'
    checksum = write_bundle(source, version, seen)
    register(root, source, version, checksum, {'source_sha256': 'test-trainer'})
    return checksum


def activate(root, registry, version, checksum):
    request = activation(version, checksum, registry.snapshot().default.sha256)
    publish_activation(root, request)
    registry.refresh()
    return request


def test_pins_and_inflight_snapshots_keep_models_across_switch(tmp_path):
    old = write_bundle(tmp_path / 'model.joblib', 'A')
    second = install(tmp_path, 'B', 1)
    candidate = install(tmp_path, 'C', 2)
    registry = ModelRegistry(tmp_path / 'model.joblib', 'legacy', pins={'control': 'B', 'treatment': 'B'})
    held = registry.snapshot()
    assert held.default.sha256 == old
    assert held.pins['control'] is held.pins['treatment']
    assert held.pins['treatment'].sha256 == second
    request = activate(tmp_path, registry, 'C', candidate)
    switched = registry.snapshot()
    assert switched.default.sha256 == candidate
    assert switched.activation_id == request['activation_id']
    assert switched.activation_status == 'loaded'
    assert switched.default.provenance['source_sha256'] == 'test-trainer'
    assert switched.previous is held.default
    assert switched.pins is held.pins


def test_immutable_registration_rejects_duplicate_version(tmp_path):
    install(tmp_path, 'A')
    source = tmp_path / 'different.joblib'
    checksum = write_bundle(source, 'A', 1)
    with pytest.raises(ValueError, match='different bytes'):
        register(tmp_path, source, 'A', checksum, {})
    with pytest.raises(ValueError, match='checksum'):
        register(tmp_path, source, 'B', '0' * 64, {})


@pytest.mark.parametrize('defect', ['changed', 'corrupt', 'incompatible', 'symlink', 'wrong_base'])
def test_bad_activation_keeps_last_ready_model(tmp_path, defect):
    write_bundle(tmp_path / 'model.joblib', 'A')
    source = tmp_path / 'input.joblib'
    if defect == 'corrupt':
        source.write_bytes(b'not a serialized bundle')
    elif defect == 'incompatible':
        joblib.dump({'popularity': []}, source)
    else:
        write_bundle(source, 'B')
    checksum = digest(source)
    register(tmp_path, source, 'B', checksum, {})
    target = artifact_path(tmp_path, checksum)
    if defect == 'changed':
        target.write_bytes(b'changed after registration')
    if defect == 'symlink':
        target.unlink()
        target.symlink_to(source)
    registry = ModelRegistry(tmp_path / 'model.joblib', 'legacy')
    before = registry.snapshot()
    request = activation('B', checksum, '0' * 64 if defect == 'wrong_base' else before.default.sha256)
    publish_activation(tmp_path, request)
    registry.refresh()
    after = registry.snapshot()
    assert after.default is before.default
    assert after.activation_id == request['activation_id']
    assert after.activation_status == 'failed'
    assert after.activation_error


def test_concurrent_staging_discards_superseded_work(tmp_path):
    write_bundle(tmp_path / 'model.joblib', 'A')
    candidate = install(tmp_path, 'B')
    latest = install(tmp_path, 'C')
    registry = ModelRegistry(tmp_path / 'model.joblib', 'legacy')
    start, release = Event(), Event()
    load = registry._registered

    def blocked(*args):
        start.set()
        assert release.wait(5)
        return load(*args)

    request = activation('B', candidate, registry.snapshot().default.sha256)
    publish_activation(tmp_path, request)
    worker = Thread(target=registry.refresh)
    with patch.object(registry, '_registered', side_effect=blocked) as mocked:
        worker.start()
        try:
            assert start.wait(5)
            registry.refresh()
            assert mocked.call_count == 1
            publish_activation(tmp_path, activation('C', latest, request['base_sha256']))
        finally:
            release.set()
            worker.join(5)
    assert registry.snapshot().default.version == 'A'
    registry.refresh()
    assert registry.snapshot().default.version == 'C'


def test_capacity_counts_inflight_handles(tmp_path):
    write_bundle(tmp_path / 'model.joblib', 'A')
    registry = ModelRegistry(tmp_path / 'model.joblib', 'legacy', capacity=3)
    inflight = registry.snapshot()
    for version in ('B', 'C'):
        activate(tmp_path, registry, version, install(tmp_path, version))
    newest = install(tmp_path, 'D')
    activate(tmp_path, registry, 'D', newest)
    assert registry.snapshot().default.version == 'C'
    assert registry.snapshot().activation_status == 'failed'
    del inflight
    activate(tmp_path, registry, 'D', newest)
    assert registry.snapshot().default.version == 'D'


@pytest.mark.parametrize('interrupt', ['before_publication', 'after_ack', 'after_finish'])
def test_rollback_recovers_transaction_and_alias(tmp_path, interrupt):
    live = tmp_path / 'model.joblib'
    champion = write_bundle(live, 'A')
    day = date(2026, 1, 3)
    directory = tmp_path / 'daily' / str(day)
    directory.mkdir(parents=True)
    candidate = write_bundle(directory / 'candidate.joblib', 'B', 1)
    write_json(directory / 'report.json', dict(status='awaiting_approval',
               eligible_for_manual_promotion=True, model_version='B',
               champion_sha256=champion, candidate_sha256=candidate))
    registry = ModelRegistry(live, 'legacy')
    request = stage(tmp_path, day)
    assert digest(live) == champion
    if interrupt == 'before_publication':
        (tmp_path / ACTIVATION_FILE).unlink()
    else:
        # A new worker can reconcile a pending instruction using the old alias.
        registry = ModelRegistry(live, 'legacy')
        registry.refresh()
        assert registry.snapshot().default.sha256 == candidate
        with pytest.raises(ValueError, match='acknowledgement'):
            finish(tmp_path, day, request['activation_id'], champion)
        if interrupt == 'after_finish':
            finish(tmp_path, day, request['activation_id'], candidate)
            finish(tmp_path, day, request['activation_id'], candidate)
            assert digest(live) == candidate
    rollback = request_rollback(tmp_path, day)
    assert request_rollback(tmp_path, day) == rollback
    registry.refresh()
    state = registry.snapshot()
    assert state.activation_id == rollback['activation_id']
    assert state.default.sha256 == champion
    finish(tmp_path, day, state.activation_id, champion, rollback=True)
    finish(tmp_path, day, state.activation_id, champion, rollback=True)
    assert digest(live) == champion
    assert not (tmp_path / 'promotion-pending.json').exists()
    assert '"rolled_back"' in (directory / 'promotion.json').read_text()
