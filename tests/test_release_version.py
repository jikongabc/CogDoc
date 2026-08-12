import pytest

from scripts.check_release_version import ROOT, check, versions


def test_release_version_sources_agree():
    declared = versions()
    assert set(declared.values()) == {"0.1.0"}
    assert check(tag="v0.1.0") == "0.1.0"


def test_release_version_rejects_mismatched_tag():
    with pytest.raises(RuntimeError, match="does not match"):
        check(tag="v9.9.9")


def test_api_uses_runtime_package_version():
    from cogdoc import __version__
    from cogdoc.api.app import app

    assert app.version == __version__


def test_release_workflow_preserves_ops_layout_and_verifies_recursive_checksums():
    workflow = (ROOT / ".github/workflows/release-artifacts.yml").read_text(
        encoding="utf-8"
    )
    assert "mkdir -p release/scripts" in workflow
    assert "release/scripts/" in workflow
    assert "find . -type f ! -name SHA256SUMS -print0" in workflow
    assert workflow.count("sha256sum --check --strict SHA256SUMS") >= 2
    assert 'assert "/tmp/cogdoc-release-venv/" in rust_core.__file__' in workflow
    assert 'assert "/tmp/cogdoc-sdist-venv/" in rust_core.__file__' in workflow


def test_docker_runtime_uses_persistent_backup_dir_and_readiness_healthcheck():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COGDOC_BACKUP_DIR=/app/data/backups" in dockerfile
    assert "mkdir -p /app/data/backups" in dockerfile
    assert "http://127.0.0.1:8000/readyz" in dockerfile
    assert "http://127.0.0.1:8000/healthz" not in dockerfile
