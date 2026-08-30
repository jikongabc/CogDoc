import zipfile
from types import SimpleNamespace

import pytest

from cogdoc.config.settings import Settings
from cogdoc.tools.source_parser import (
    SourceParseError,
    UnsafeSourceArchiveError,
    parse_source,
    scan_source_manifest,
)


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def _settings(**values):
    return Settings(_env_file=None, **values)


def test_markdown_and_html_preserve_structure_and_tables(tmp_path):
    markdown = tmp_path / "guide.md"
    markdown.write_text(
        "# 标题\n\n正文\n\n| A | B |\n|---|---|\n| 1 | 2 |", encoding="utf-8"
    )
    html = tmp_path / "page.html"
    html.write_text(
        "<h1>Title</h1><script>secret()</script><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
        encoding="utf-8",
    )
    assert "| A | B |" in parse_source(str(markdown))[0]["text"]
    html_text = parse_source(str(html))[0]["text"]
    assert "# Title" in html_text and "| A | B |" in html_text
    assert "secret" not in html_text


def test_docx_extracts_paragraphs_and_tables(tmp_path):
    path = tmp_path / "report.docx"
    _zip(
        path,
        {
            "word/document.xml": """<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>Heading</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"""
        },
    )
    text = parse_source(str(path))[0]["text"]
    assert "Heading" in text and "| A | B |" in text


def test_docx_preserves_heading_style_with_real_ooxml_namespace(tmp_path):
    path = tmp_path / "headed.docx"
    namespace = (
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    )
    _zip(
        path,
        {
            "word/document.xml": (
                f"<w:document xmlns:w='{namespace}'><w:body>"
                "<w:p><w:pPr><w:pStyle w:val='Heading2'/></w:pPr>"
                "<w:r><w:t>Architecture</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Details</w:t></w:r></w:p>"
                "</w:body></w:document>"
            )
        },
    )

    assert parse_source(str(path))[0]["text"].startswith(
        "## Architecture\n\nDetails"
    )


def test_pptx_returns_one_location_per_slide(tmp_path):
    path = tmp_path / "deck.pptx"

    def slide(value):
        return f"<p:sld xmlns:p='p' xmlns:a='a'><a:t>{value}</a:t></p:sld>"

    _zip(
        path,
        {
            "ppt/slides/slide2.xml": slide("Second"),
            "ppt/slides/slide1.xml": slide("First"),
        },
    )
    pages = parse_source(str(path))
    assert [page["text"] for page in pages] == ["First", "Second"]
    assert pages[1]["location"] == {"slide": 2}


def test_pptx_uses_presentation_relationship_order(tmp_path):
    path = tmp_path / "reordered.pptx"
    _zip(
        path,
        {
            "ppt/presentation.xml": (
                "<p:presentation xmlns:p='presentation' xmlns:r='relationships'>"
                "<p:sldIdLst><p:sldId id='256' r:id='r2'/>"
                "<p:sldId id='257' r:id='r1'/></p:sldIdLst>"
                "</p:presentation>"
            ),
            "ppt/_rels/presentation.xml.rels": (
                "<Relationships><Relationship Id='r1' "
                "Target='slides/slide1.xml'/><Relationship Id='r2' "
                "Target='slides/slide2.xml'/></Relationships>"
            ),
            "ppt/slides/slide1.xml": "<p:sld xmlns:p='p'><p:t>First</p:t></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld xmlns:p='p'><p:t>Second</p:t></p:sld>",
        },
    )

    pages = parse_source(str(path))

    assert [page["text"] for page in pages] == ["Second", "First"]
    assert [page["location"] for page in pages] == [{"slide": 1}, {"slide": 2}]


def test_pptx_does_not_index_orphan_slide_parts(tmp_path):
    path = tmp_path / "empty-with-orphan.pptx"
    _zip(
        path,
        {
            "ppt/presentation.xml": (
                "<p:presentation xmlns:p='presentation'><p:sldIdLst/>"
                "</p:presentation>"
            ),
            "ppt/_rels/presentation.xml.rels": "<Relationships/>",
            "ppt/slides/slide1.xml": (
                "<p:sld xmlns:p='p'><p:t>Deleted confidential slide</p:t></p:sld>"
            ),
        },
    )

    assert parse_source(str(path)) == []


def test_pptx_preserves_tables_as_markdown(tmp_path):
    path = tmp_path / "table.pptx"
    _zip(
        path,
        {
            "ppt/slides/slide1.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:tbl><a:tr><a:tc><a:t>A</a:t></a:tc><a:tc><a:t>B</a:t></a:tc></a:tr></a:tbl></p:sld>"
        },
    )
    assert "| A | B |" in parse_source(str(path))[0]["text"]


def test_xlsx_resolves_shared_strings_and_sheet_location(tmp_path):
    path = tmp_path / "book.xlsx"
    _zip(
        path,
        {
            "xl/workbook.xml": "<workbook xmlns:r='rel'><sheets><sheet name='Data' r:id='r1'/></sheets></workbook>",
            "xl/_rels/workbook.xml.rels": "<Relationships><Relationship Id='r1' Target='worksheets/sheet1.xml'/></Relationships>",
            "xl/sharedStrings.xml": "<sst><si><t>Name</t></si><si><t>Alice</t></si></sst>",
            "xl/worksheets/sheet1.xml": "<worksheet><sheetData><row><c r='A1' t='s'><v>0</v></c><c r='C1' t='s'><v>1</v></c></row></sheetData></worksheet>",
        },
    )
    page = parse_source(str(path))[0]
    assert page["location"] == {"sheet": "Data"}
    assert "| Name |  | Alice |" in page["text"]


def test_image_ocr_is_bounded_and_uses_stdin(tmp_path):
    path = tmp_path / "scan.png"
    path.write_bytes(b"png")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="扫描内容", stderr=b"")

    settings = _settings(cogdoc_ocr_enabled=True, cogdoc_ocr_binary="/bin/true")
    page = parse_source(str(path), settings=settings, image_ocr_runner=runner)[0]
    assert page["text"] == "扫描内容" and page["extraction_method"] == "ocr"
    assert calls[0][1]["input"] == b"png" and "shell" not in calls[0][1]


def test_archive_entities_and_invalid_zip_fail_closed(tmp_path):
    unsafe = tmp_path / "unsafe.docx"
    _zip(unsafe, {"word/document.xml": "<!DOCTYPE x [<!ENTITY a 'boom'>]><x>&a;</x>"})
    with pytest.raises(UnsafeSourceArchiveError):
        parse_source(str(unsafe))
    invalid = tmp_path / "invalid.docx"
    invalid.write_bytes(b"not zip")
    with pytest.raises(SourceParseError):
        parse_source(str(invalid))


def test_xlsx_relationship_cannot_escape_worksheet_directory(tmp_path):
    path = tmp_path / "unsafe.xlsx"
    _zip(
        path,
        {
            "xl/workbook.xml": "<workbook xmlns:r='rel'><sheets><sheet name='Data' r:id='r1'/></sheets></workbook>",
            "xl/_rels/workbook.xml.rels": "<Relationships><Relationship Id='r1' Target='../secret.xml'/></Relationships>",
            "secret.xml": "<worksheet/>",
        },
    )
    with pytest.raises(UnsafeSourceArchiveError):
        parse_source(str(path))


def test_manifest_scans_all_supported_formats_deterministically(tmp_path):
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "ignored.exe").write_bytes(b"x")
    manifest = scan_source_manifest("kb", str(tmp_path))
    assert [row["name"] for row in manifest["documents"]] == ["a.txt", "b.md"]
