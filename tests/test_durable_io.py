import json
import os

import pytest

from cogdoc.service.durable_io import atomic_write_json


def test_atomic_write_json_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    target = tmp_path / "control.json"
    calls = []
    real_fsync = os.fsync

    def tracking_fsync(descriptor):
        calls.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    atomic_write_json(str(target), {"epoch": 7})

    assert json.loads(target.read_text(encoding="utf-8")) == {"epoch": 7}
    assert len(calls) >= 2
    assert not list(tmp_path.glob("control.json.*.tmp"))


def test_atomic_write_json_preserves_old_value_when_replace_fails(
    tmp_path, monkeypatch
):
    target = tmp_path / "control.json"
    target.write_text('{"epoch":1}', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        atomic_write_json(str(target), {"epoch": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"epoch": 1}
    assert not list(tmp_path.glob("control.json.*.tmp"))
