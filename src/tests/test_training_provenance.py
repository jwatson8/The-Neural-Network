import gzip
import hashlib

import pytest

from src.model_updates.provenance import retain_original_data


def test_retained_inputs_survive_source_changes_and_detect_corruption(tmp_path):
    data, artifacts = tmp_path / 'data', tmp_path / 'artifacts'
    data.mkdir()
    artifacts.mkdir()
    source = data / 'events.csv.gz'
    source.write_bytes(gzip.compress(b'original training rows'))
    manifest = retain_original_data(data, artifacts)
    original = source.read_bytes()
    retained = artifacts / manifest['events.csv.gz']['path']
    assert retained.read_bytes() == original
    assert manifest['events.csv.gz']['sha256'] == hashlib.sha256(original).hexdigest()
    source.write_bytes(gzip.compress(b'new rows'))
    second = retain_original_data(data, artifacts)
    assert second != manifest
    assert retained.read_bytes() == original
    retained.write_bytes(b'corruption')
    source.write_bytes(original)
    with pytest.raises(ValueError, match='checksum'):
        retain_original_data(data, artifacts)
