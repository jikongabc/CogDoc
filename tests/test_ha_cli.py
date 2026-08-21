from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from cogdoc.ha import cli
from cogdoc.ha.runtime import HAConfig, HARuntime
from cogdoc.ha.tasks import JOB_DEAD_LETTER


def _config(tmp_path) -> HAConfig:
    return HAConfig(
        enabled=True,
        database_url="",
        database_schema="cogdoc",
        object_store="local",
        object_root=str(tmp_path / "objects"),
        s3_bucket="",
        s3_prefix="cogdoc",
        s3_endpoint_url=None,
        s3_region=None,
        s3_require_versioning=True,
        worker_id="cli-worker",
        scheduler_enabled=False,
        outbox_enabled=False,
    )


def _publish_args(directory, build_id):
    return argparse.Namespace(
        tenant="tenant",
        kb="kb",
        build_id=build_id,
        directory=str(directory),
        chunk_version="v1",
        embedding_model="model",
        dimensions=3,
        lease_seconds=300.0,
    )


def test_cli_publish_replay_and_gc_keep_current_generation(tmp_path, monkeypatch):
    runtime = HARuntime(_config(tmp_path))
    directory = tmp_path / "build"
    directory.mkdir()
    index_file = directory / "index.bin"
    index_file.write_bytes(b"first")

    assert cli._publish(runtime, _publish_args(directory, "build-1")) == 0
    first = runtime.index_generations.current("tenant", "kb")
    assert first is not None
    assert cli._publish(runtime, _publish_args(directory, "build-1")) == 0

    index_file.write_bytes(b"second")
    assert cli._publish(runtime, _publish_args(directory, "build-2")) == 0
    current = runtime.index_generations.current("tenant", "kb")
    assert current is not None
    assert current["generation_id"] != first["generation_id"]

    monkeypatch.setattr(cli.time, "time", lambda: 10**12)
    assert cli._gc(runtime, SimpleNamespace(retention_seconds=1.0, limit=100)) == 0
    assert runtime.index_generations.get(first["generation_id"]) is None
    assert (
        runtime.index_generations.resolve_current(
            "tenant", "kb", runtime.index_repository.verify
        )["generation_id"]
        == current["generation_id"]
    )
    runtime.shutdown()


def test_cli_scrub_stream_verifies_current_generation(tmp_path, capsys):
    runtime = HARuntime(_config(tmp_path))
    directory = tmp_path / "build"
    directory.mkdir()
    (directory / "index.bin").write_bytes(b"verified")
    assert cli._publish(runtime, _publish_args(directory, "build")) == 0
    capsys.readouterr()

    assert cli._scrub(runtime, argparse.Namespace(limit=100)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "checked": 1,
        "status": "verified",
    }
    runtime.shutdown()


def test_cli_enqueue_prepares_generation_for_worker_handoff(tmp_path, capsys):
    runtime = HARuntime(_config(tmp_path))
    directory = tmp_path / "build"
    directory.mkdir()
    (directory / "index.bin").write_bytes(b"prepared")

    assert cli._enqueue_index(runtime, _publish_args(directory, "build")) == 0
    output = json.loads(capsys.readouterr().out)
    generation = runtime.index_generations.get(output["generation_id"])
    assert generation is not None
    assert generation["status"] == "prepared"
    assert runtime.index_worker is not None
    assert runtime.index_worker.run_once()
    assert (
        runtime.index_generations.current("tenant", "kb")["generation_id"]
        == (output["generation_id"])
    )
    runtime.shutdown()


def test_cli_replays_dead_letter_with_operator_idempotency_key(
    tmp_path, monkeypatch, capsys
):
    runtime = HARuntime(_config(tmp_path))
    original = runtime.jobs.enqueue("work", "tenant", {}, max_attempts=1)
    lease = runtime.jobs.claim("work", "worker", lease_seconds=1)
    assert lease is not None
    monkeypatch.setattr(runtime.jobs, "_clock", lambda: 10**12)
    assert runtime.jobs.reap_expired() == 1
    assert runtime.jobs.get(original["job_id"])["status"] == JOB_DEAD_LETTER
    monkeypatch.setattr(cli, "_runtime", lambda: runtime)

    assert (
        cli.main(
            [
                "replay-job",
                "--job-id",
                original["job_id"],
                "--replay-key",
                "incident-42",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["replay_of"] == original["job_id"]
    assert output["status"] == "queued"


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "publish-index",
            "--tenant",
            "t",
            "--kb",
            "k",
            "--build-id",
            "b",
            "--directory",
            ".",
            "--chunk-version",
            "v",
            "--embedding-model",
            "m",
            "--dimensions",
            "0",
        ],
        [
            "publish-index",
            "--tenant",
            "t",
            "--kb",
            "k",
            "--build-id",
            "b",
            "--directory",
            ".",
            "--chunk-version",
            "v",
            "--embedding-model",
            "m",
            "--dimensions",
            "3",
            "--lease-seconds",
            "1",
        ],
        ["gc-index", "--retention-seconds", "0"],
        ["gc-index", "--limit", "1001"],
    ],
)
def test_cli_rejects_unsafe_operational_bounds(arguments):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(arguments)


@pytest.mark.parametrize("command", ["doctor", "scheduler-once", "migrate"])
def test_cli_local_operational_commands_close_owned_runtime(
    command, tmp_path, monkeypatch, capsys
):
    created = []

    def runtime_factory():
        runtime = HARuntime(_config(tmp_path / command))
        created.append(runtime)
        return runtime

    monkeypatch.setattr(cli, "_runtime", runtime_factory)
    assert cli.main([command]) == 0
    output = json.loads(capsys.readouterr().out)
    if command == "doctor":
        assert output["status"] == "ready"
        assert output["multi_instance_safe"] is False
    elif command == "scheduler-once":
        assert output == {"delivered": 0, "fires": 0}
    else:
        assert [(row["version"], row["phase"]) for row in output["migrations"]] == [
            (1, "validated")
        ]
    with pytest.raises(Exception, match="closed"):
        created[0].backend.check()
