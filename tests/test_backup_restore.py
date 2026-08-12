import copy
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from cogdoc.config.settings import get_settings
from scripts import backup_state
from scripts.backup_state import (
    DEFAULT_BACKUP_DIR,
    MANIFEST_NAME,
    ROOT,
    _arcname,
    _default_backup_dir,
    create_backup,
)
from scripts.restore_state import RestoreError, restore_archive


@pytest.fixture
def state_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    data = tmp_path / "source-state"
    (data / "nested").mkdir(parents=True)
    (data / "nested" / "record.json").write_text('{"value": 1}', encoding="utf-8")
    (data / "empty").mkdir()
    monkeypatch.setenv("COGDOC_DATA_DIR", str(data))
    get_settings.cache_clear()
    archive = create_backup(
        tmp_path / "backups",
        name="state.tar.gz",
        include_traces=False,
        include_env=False,
        extra_paths=[],
    )
    yield archive, data
    get_settings.cache_clear()


def test_backup_manifest_and_verify(state_backup: tuple[Path, Path]) -> None:
    archive, _ = state_backup
    with tarfile.open(archive, "r:gz") as bundle:
        manifest_stream = bundle.extractfile(MANIFEST_NAME)
        assert manifest_stream is not None
        manifest = json.load(manifest_stream)
    assert manifest["schema_version"] == "v2"
    assert manifest["file_count"] == 1
    assert manifest["files"][0]["path"].endswith("nested/record.json")
    assert len(manifest["files"][0]["sha256"]) == 64
    assert manifest["files"][0]["created_at"]
    assert manifest["options"]["includes_env"] is False
    result = restore_archive(archive, verify_only=True)
    assert result["operation"] == "verify"
    assert result["verification_level"] == "full"
    assert result["degraded"] is False


def test_backup_cli_defaults_to_text_and_json_is_opt_in(
    state_backup: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup_state.py",
            "--output-dir",
            str(tmp_path / "text-backup"),
            "--name",
            "text",
            "--no-traces",
        ],
    )
    assert backup_state.main() == 0
    text_output = capsys.readouterr().out
    assert "备份完成:" in text_output
    assert "大小:" in text_output
    assert "恢复提示:" in text_output

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup_state.py",
            "--output-dir",
            str(tmp_path / "json-backup"),
            "--name",
            "json",
            "--no-traces",
            "--json",
        ],
    )
    assert backup_state.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["operation"] == "backup"
    assert result["archive"].endswith("json.tar.gz")


def test_root_arcname_remains_dot() -> None:
    assert _arcname(ROOT) == "."


def test_backup_default_dir_can_be_overridden_for_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COGDOC_BACKUP_DIR", raising=False)
    assert _default_backup_dir() == DEFAULT_BACKUP_DIR

    configured = tmp_path / "persistent-backups"
    monkeypatch.setenv("COGDOC_BACKUP_DIR", str(configured))
    assert _default_backup_dir() == configured


def test_backup_output_nested_in_data_is_not_recursively_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.db").write_bytes(b"state")
    output_dir = data / "backups"
    monkeypatch.setenv("COGDOC_DATA_DIR", str(data))
    monkeypatch.setenv("COGDOC_TRACE_DIR", str(data / "logs" / "traces"))
    get_settings.cache_clear()
    try:
        first = create_backup(
            output_dir,
            name="first.tar.gz",
            include_traces=True,
            include_env=False,
            extra_paths=[],
        )
        second = create_backup(
            output_dir,
            name="second.tar.gz",
            include_traces=True,
            include_env=False,
            extra_paths=[],
        )
    finally:
        get_settings.cache_clear()

    assert first.is_file()
    with tarfile.open(second, "r:gz") as bundle:
        names = bundle.getnames()
    assert any(name.endswith("data/state.db") for name in names)
    assert not any("/backups/" in f"/{name}/" for name in names)


def test_release_bundle_ops_scripts_keep_root_contract_and_support_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_root = tmp_path / "release"
    bundle_scripts = bundle_root / "scripts"
    bundle_scripts.mkdir(parents=True)
    script_names = ("backup_state.py", "restore_state.py", "migrate_state.py")
    for name in script_names:
        shutil.copy2(ROOT / "scripts" / name, bundle_scripts / name)

    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else source_path
    )
    for name in script_names:
        completed = subprocess.run(
            [sys.executable, str(bundle_scripts / name), "--help"],
            cwd=bundle_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert "usage:" in completed.stdout

    monkeypatch.delenv("COGDOC_BACKUP_DIR", raising=False)
    backup_module = runpy.run_path(str(bundle_scripts / "backup_state.py"))
    restore_module = runpy.run_path(str(bundle_scripts / "restore_state.py"))
    assert backup_module["ROOT"] == bundle_root
    assert backup_module["_default_backup_dir"]() == bundle_root / "backups"
    assert restore_module["ROOT"] == bundle_root


def test_v1_archive_restores_with_degraded_verification(tmp_path: Path) -> None:
    archive = tmp_path / "legacy-v1.tar.gz"
    content = b"legacy state"
    manifest = {
        "schema_version": "v1",
        "created_at": "2026-08-04T00:00:00+00:00",
        "archive": archive.name,
        "project_root": "/legacy/CogDoc",
        "data_dir": "./data",
        "trace_dir": "logs/traces",
        "includes_env": False,
        "items": [{"path": "data", "type": "dir", "size_bytes": len(content)}],
    }
    with tarfile.open(archive, "w:gz") as output:
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        manifest_member = tarfile.TarInfo(MANIFEST_NAME)
        manifest_member.size = len(manifest_bytes)
        output.addfile(manifest_member, io.BytesIO(manifest_bytes))
        directory = tarfile.TarInfo("data")
        directory.type = tarfile.DIRTYPE
        output.addfile(directory)
        member = tarfile.TarInfo("data/state.db")
        member.size = len(content)
        output.addfile(member, io.BytesIO(content))

    verified = restore_archive(archive, verify_only=True)
    assert verified["schema_version"] == "v1"
    assert verified["verification_level"] == "degraded"
    assert verified["degraded"] is True
    assert "逐文件哈希" in verified["warning"]
    target = tmp_path / "legacy-restored"
    restored = restore_archive(archive, target)
    assert restored["degraded"] is True
    assert (target / "data" / "state.db").read_bytes() == content


def test_restore_rejects_tampered_file(state_backup: tuple[Path, Path], tmp_path: Path) -> None:
    archive, _ = state_backup
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as output:
        for original in source.getmembers():
            member = copy.copy(original)
            if original.isfile():
                stream = source.extractfile(original)
                assert stream is not None
                data = stream.read()
                if original.name.endswith("record.json"):
                    data += b"tampered"
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
            else:
                output.addfile(member)
    with pytest.raises(RestoreError, match="校验失败") as exc_info:
        restore_archive(tampered, verify_only=True)
    assert exc_info.value.code == "INTEGRITY_ERROR"


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../escape.txt")
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(RestoreError) as exc_info:
        restore_archive(archive, verify_only=True)
    assert exc_info.value.code == "UNSAFE_ARCHIVE"
    assert not (tmp_path.parent / "escape.txt").exists()


def test_restore_refuses_nonempty_target_without_force(
    state_backup: tuple[Path, Path], tmp_path: Path
) -> None:
    archive, _ = state_backup
    target = tmp_path / "restore"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(RestoreError) as exc_info:
        restore_archive(archive, target)
    assert exc_info.value.code == "TARGET_NOT_EMPTY"
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_backup_restore_round_trip(state_backup: tuple[Path, Path], tmp_path: Path) -> None:
    archive, source = state_backup
    target = tmp_path / "restored"
    result = restore_archive(archive, target)
    restored_root = target / source.name
    assert result["operation"] == "restore"
    assert (restored_root / "nested" / "record.json").read_bytes() == (
        source / "nested" / "record.json"
    ).read_bytes()
    assert (restored_root / "empty").is_dir()
