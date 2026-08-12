import json
import re

import pytest

from cogdoc.api.ingest import (
    KBExistsError,
    KnowledgeBaseRegistry,
    RegistryCorruptError,
)


def _registry(tmp_path):
    return KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(
            tmp_path / "kb" / storage_id / "sources"
        ),
    )


def test_default_tenant_keeps_legacy_identity_and_call_semantics(tmp_path):
    registry = _registry(tmp_path)

    record = registry.create("papers")

    assert record == registry.get("papers")
    assert record["tenant_id"] == "default"
    assert record["owner_id"] == "default"
    assert record["storage_id"] == "papers"
    assert registry.storage_id_for("papers") == "papers"
    assert registry.resolve("papers") == record
    assert registry.exists("papers")
    assert registry.get_by_storage_id("papers") == record
    assert registry.source_dir("papers") == str(
        tmp_path / "kb" / "papers" / "sources"
    )
    assert registry.list() == [record]
    assert registry.list(tenant_id="default") == [record]

    with pytest.raises(KBExistsError):
        registry.create("papers")


def test_same_slug_is_isolated_per_tenant_and_storage_id(tmp_path):
    registry = _registry(tmp_path)
    default = registry.create("shared")
    alpha = registry.create("shared", tenant_id="alpha", owner_id="alice")
    beta = registry.create("shared", tenant_id="beta", owner_id="bob")

    assert len({default["storage_id"], alpha["storage_id"], beta["storage_id"]}) == 3
    assert default["storage_id"] == "shared"
    assert re.fullmatch(r"t-[0-9a-f]{64}", alpha["storage_id"])
    assert re.fullmatch(r"t-[0-9a-f]{64}", beta["storage_id"])
    assert registry.storage_id_for("shared", "alpha") == alpha["storage_id"]
    assert registry.resolve("shared", "alpha") == alpha
    assert registry.get("shared") == default
    assert registry.get("shared", "alpha") == alpha
    assert registry.get("shared", "beta") == beta
    assert registry.exists("shared", "alpha")
    assert registry.get(alpha["storage_id"]) == alpha
    assert registry.exists(alpha["storage_id"])
    assert registry.list(tenant_id="alpha") == [alpha]
    assert {item["storage_id"] for item in registry.list()} == {
        default["storage_id"],
        alpha["storage_id"],
        beta["storage_id"],
    }

    alpha_path = registry.source_dir("shared", "alpha")
    assert alpha_path == registry.source_dir(alpha["storage_id"])
    assert alpha_path != registry.source_dir("shared", "beta")

    assert registry.delete("shared", "alpha") is True
    assert registry.get("shared", "alpha") is None
    assert registry.get_by_storage_id(alpha["storage_id"]) is None
    assert registry.exists("shared")
    assert registry.exists("shared", "beta")
    assert registry.delete("shared", "alpha") is False
    assert registry.delete(beta["storage_id"]) is True
    assert not registry.exists(beta["storage_id"])
    assert registry.exists("shared")


def test_storage_identity_is_stable_across_registry_reload(tmp_path):
    registry = _registry(tmp_path)
    created = registry.create("papers", tenant_id="team-a", owner_id="alice")
    storage_id = created["storage_id"]

    persisted = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert list(persisted) == [storage_id]
    assert persisted[storage_id]["storage_id"] == storage_id

    reloaded = _registry(tmp_path)
    assert reloaded.resolve("papers", "team-a") == created
    assert reloaded.get("papers", "team-a") == created
    assert reloaded.get_by_storage_id(storage_id) == created
    assert reloaded.storage_id_for("papers", "team-a") == storage_id


def test_legacy_registry_is_normalized_in_memory_without_rewrite(tmp_path):
    path = tmp_path / "registry.json"
    legacy_text = json.dumps(
        {"legacy": {"kb_id": "legacy", "created_at": "old"}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    path.write_text(legacy_text, encoding="utf-8")

    registry = _registry(tmp_path)

    assert path.read_text(encoding="utf-8") == legacy_text
    assert registry.get("legacy") == {
        "kb_id": "legacy",
        "created_at": "old",
        "tenant_id": "default",
        "owner_id": "default",
        "storage_id": "legacy",
    }
    assert registry.resolve("legacy", "default") == registry.get("legacy")
    assert registry.source_dir("legacy") == str(
        tmp_path / "kb" / "legacy" / "sources"
    )


@pytest.mark.parametrize(
    ("kb_id", "tenant_id"),
    [
        ("../escape", "default"),
        ("a/b", "default"),
        ("a\\b", "default"),
        (".", "default"),
        ("valid", "tenant\nname"),
    ],
)
def test_dangerous_logical_ids_are_rejected(tmp_path, kb_id, tenant_id):
    registry = _registry(tmp_path)

    with pytest.raises(ValueError):
        registry.create(kb_id, tenant_id=tenant_id)

    assert not (tmp_path / "escape").exists()
    assert registry.list() == []


def test_nondefault_storage_id_is_safe_and_unambiguous(tmp_path):
    registry = _registry(tmp_path)

    first = registry.storage_id_for("bc", "a")
    second = registry.storage_id_for("c", "ab")
    unicode_id = registry.storage_id_for("论文库", "研发组@example.com")
    dangerous_tenant_id = registry.storage_id_for("papers", "../../tenant/team")

    assert first != second
    assert re.fullmatch(r"t-[0-9a-f]{64}", unicode_id)
    assert re.fullmatch(r"t-[0-9a-f]{64}", dangerous_tenant_id)
    assert "/" not in dangerous_tenant_id and "\\" not in dangerous_tenant_id


def test_dangerous_legacy_storage_key_fails_closed(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"../escape": {"kb_id": "../escape"}}), encoding="utf-8"
    )

    with pytest.raises(RegistryCorruptError):
        _registry(tmp_path)

    assert not path.exists()
    assert list(tmp_path.glob("registry.json.corrupt-*"))
