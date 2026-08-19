import hashlib

from cogdoc.service.source_model import SourceDocument
from cogdoc.tools.chunker import chunk_paper
from cogdoc.tools.source_parser import parse_source


def test_chunk_carries_format_neutral_source_location():
    pages = [
        {
            "page": 1,
            "source": "book.xlsx",
            "text": "| Name | Value |\n| --- | --- |\n| alpha | 42 |",
            "location": {"sheet": "Data", "cell_range": "A1:B2"},
        }
    ]
    chunks = chunk_paper(pages, hashlib.sha256(b"xlsx").hexdigest())
    assert chunks[0]["meta"]["source_location"] == {
        "sheet": "Data",
        "cell_range": "A1:B2",
    }
    assert chunks[0]["meta"]["sheet"] == "Data"


def test_parser_and_chunker_carry_source_version_identity(tmp_path):
    path = tmp_path / "guide.md"
    raw = b"# Guide\n\nThis is enough meaningful source content for one stable chunk."
    path.write_bytes(raw)
    document = SourceDocument.create(
        connector_type="git",
        external_id="docs/guide.md",
        display_name="guide.md",
        content_sha256=hashlib.sha256(raw).hexdigest(),
    )
    pages = parse_source(str(path), source_document=document)
    chunks = chunk_paper(pages, document.version.content_sha256)
    assert chunks[0]["meta"]["source_id"] == document.source_id
    assert chunks[0]["meta"]["source_version_id"] == document.version.version_id
    assert chunks[0]["meta"]["connector_type"] == "git"
