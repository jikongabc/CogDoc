import hashlib

import pytest

from cogdoc.service.source_model import (
    SOURCE_CONTRACT_VERSION,
    SourceDocument,
    SourceLocation,
    build_source_id,
    canonical_origin_uri,
    stamp_source_contract,
)


HASH = hashlib.sha256(b"hello").hexdigest()


def test_legacy_manifest_projects_to_generic_source_contract():
    manifest = stamp_source_contract(
        {"doc_id": "kb", "documents": [{"name": "论文.pdf", "size": 5, "sha256": HASH}]}
    )
    row = manifest["documents"][0]
    assert manifest["source_contract_version"] == SOURCE_CONTRACT_VERSION
    assert row["name"] == "论文.pdf" and row["sha256"] == HASH
    assert row["connector_type"] == "legacy-upload"
    assert row["source_id"] == build_source_id("legacy-upload", "论文.pdf")
    assert row["version_id"].startswith("sv-")
    assert row["media_type"] == "application/pdf"


def test_source_identity_survives_display_name_change():
    first = SourceDocument.create(
        connector_type="git",
        external_id="docs/readme.md",
        display_name="README.md",
        content_sha256=HASH,
    )
    renamed = SourceDocument.create(
        connector_type="git",
        external_id="docs/readme.md",
        display_name="指南.md",
        content_sha256=HASH,
    )
    assert first.source_id == renamed.source_id
    assert first.version.version_id == renamed.version.version_id


def test_origin_uri_strips_credentials_and_fragment():
    assert canonical_origin_uri("https://user:secret@example.com:8443/a?q=1#token") == (
        "https://example.com:8443/a"
    )


def test_source_location_validates_format_neutral_coordinates():
    location = SourceLocation(
        sheet="Sheet 1", cell_range="A1:C4", section_path=("结果",)
    )
    assert SourceLocation.from_dict(location.to_dict()) == location
    with pytest.raises(ValueError, match="cell_range requires sheet"):
        SourceLocation(cell_range="A1")
    with pytest.raises(ValueError, match="page_end"):
        SourceLocation(page_start=3, page_end=2)
    with pytest.raises(ValueError, match="section_path"):
        SourceLocation.from_dict({"section_path": "not-a-list"})


def test_source_metadata_must_be_json_serializable():
    with pytest.raises(TypeError):
        SourceDocument.create(
            connector_type="web",
            external_id="x",
            display_name="x.html",
            content_sha256=HASH,
            metadata={"unsafe": object()},
        )
