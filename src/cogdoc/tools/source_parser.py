from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import re
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, cast
from xml.etree import ElementTree as ET

from cogdoc.config.settings import Settings, get_settings
from cogdoc.graph.state import ParsedPage
from cogdoc.tools.ocr import (
    MAX_OCR_OUTPUT_BYTES,
    OcrConfig,
    OcrExecutionError,
    OcrTimeoutError,
    OcrUnavailableError,
    normalize_ocr_text,
    probe_ocr_dependency,
)
from cogdoc.tools.parser import smart_parse

if TYPE_CHECKING:
    from cogdoc.source_model import SourceDocument


SOURCE_PARSER_VERSION = "unified-source-parser-v2"
CONNECTOR_MATERIALIZED_PREFIX = ".cogdoc-connector-"
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_XML_PART_BYTES = 32 * 1024 * 1024
MAX_XML_NODES = 250_000

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".md",
        ".markdown",
        ".txt",
        ".html",
        ".htm",
        ".docx",
        ".pptx",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".tif",
        ".tiff",
        ".bmp",
    }
)


class SourceParseError(RuntimeError):
    pass


class UnsupportedSourceError(SourceParseError):
    pass


class UnsafeSourceArchiveError(SourceParseError):
    pass


def list_supported_files(source_dir: str) -> list[str]:
    return sorted(
        name
        for name in os.listdir(source_dir)
        if Path(name).suffix.casefold() in SUPPORTED_EXTENSIONS
        and os.path.isfile(os.path.join(source_dir, name))
    )


def scan_source_manifest(kb_id: str, source_dir: str) -> dict[str, Any]:
    documents = []
    for name in list_supported_files(source_dir):
        path = os.path.join(source_dir, name)
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            while block := handle.read(1024 * 1024):
                size += len(block)
                if size > MAX_SOURCE_BYTES:
                    raise SourceParseError(
                        f"source exceeds {MAX_SOURCE_BYTES} bytes: {name}"
                    )
                digest.update(block)
        documents.append({"name": name, "size": size, "sha256": digest.hexdigest()})
    return {
        "doc_id": kb_id,
        "doc_dir": os.path.abspath(source_dir),
        "documents": documents,
    }


def _page(source: str, number: int, text: str, **metadata: Any) -> dict[str, Any]:
    row = {
        "page": number,
        "source": source,
        "text": text.strip(),
        "is_ocr_fallback": False,
        "extraction_method": "native",
        "ocr_status": "not_needed",
        "location": metadata.pop("location", {"unit": number}),
    }
    row.update(metadata)
    return row


def annotate_parsed_pages(
    pages: Iterable[dict[str, Any]], document: SourceDocument | None
) -> list[dict[str, Any]]:
    materialized = [dict(page) for page in pages]
    if document is None:
        return materialized
    for page in materialized:
        page.update(
            {
                "source_id": document.source_id,
                "source_version_id": document.version.version_id,
                "media_type": document.media_type,
                "origin_uri": document.origin_uri,
                "connector_type": document.connector_type,
            }
        )
    return materialized


def _read_bounded(path: str) -> bytes:
    size = os.path.getsize(path)
    if size > MAX_SOURCE_BYTES:
        raise SourceParseError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    with open(path, "rb") as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise SourceParseError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    return raw


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


class _SemanticHtmlParser(HTMLParser):
    _BLOCKS = {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "li",
        "tr",
        "pre",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._table_depth = 0
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "table":
            self._table_depth += 1
            self.parts.append("\n")
        elif tag in {"td", "th"} and self._table_depth:
            self._cell = []
        elif tag == "br":
            self.parts.append("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append(f"\n{'#' * int(tag[1])} ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag in {"td", "th"} and self._table_depth and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._table_depth and self._row:
            self.parts.append("| " + " | ".join(self._row) + " |\n")
            self._row = []
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            self.parts.append("\n")
        elif tag in self._BLOCKS or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\r", "")
        value = re.sub(r"[ \t]+", " ", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def _safe_archive(path: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceParseError("invalid Office Open XML archive") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        archive.close()
        raise UnsafeSourceArchiveError("archive contains too many entries")
    total = sum(info.file_size for info in infos)
    if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        archive.close()
        raise UnsafeSourceArchiveError("archive expands beyond the safety limit")
    return archive


def _xml_part(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise SourceParseError(f"required archive part is missing: {name}") from exc
    if info.file_size > MAX_XML_PART_BYTES:
        raise UnsafeSourceArchiveError("XML part exceeds the safety limit")
    raw = archive.read(info)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise UnsafeSourceArchiveError("XML entities are not allowed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceParseError(f"invalid XML part: {name}") from exc
    if sum(1 for _ in root.iter()) > MAX_XML_NODES:
        raise UnsafeSourceArchiveError("XML part contains too many nodes")
    return root


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute_by_local_name(element: ET.Element, name: str) -> str:
    return next(
        (
            str(value)
            for key, value in element.attrib.items()
            if _local_name(key) == name
        ),
        "",
    )


def _element_text(element: ET.Element) -> str:
    pieces: list[str] = []
    for node in element.iter():
        local = _local_name(node.tag)
        if local == "t" and node.text:
            pieces.append(node.text)
        elif local in {"tab"}:
            pieces.append("\t")
        elif local in {"br", "cr"}:
            pieces.append("\n")
    return "".join(pieces).strip()


def _markdown_table(rows: Iterable[Iterable[str]]) -> str:
    materialized = [
        [str(cell).replace("|", "\\|").strip() for cell in row] for row in rows
    ]
    width = max((len(row) for row in materialized), default=0)
    if not width:
        return ""
    normalized = [row + [""] * (width - len(row)) for row in materialized]
    lines = ["| " + " | ".join(row) + " |" for row in normalized]
    if len(lines) == 1:
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    else:
        lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


def _parse_docx(path: str) -> list[dict[str, Any]]:
    source = os.path.basename(path)
    with _safe_archive(path) as archive:
        root = _xml_part(archive, "word/document.xml")
    body = next((node for node in root.iter() if _local_name(node.tag) == "body"), root)
    blocks: list[str] = []
    for child in body:
        local = _local_name(child.tag)
        if local == "p":
            text = _element_text(child)
            if text:
                style = next(
                    (
                        _attribute_by_local_name(node, "val")
                        for node in child.iter()
                        if _local_name(node.tag) == "pStyle"
                    ),
                    "",
                )
                heading = re.search(r"(?:heading|标题)\s*([1-6])", style, re.IGNORECASE)
                blocks.append(
                    f"{'#' * int(heading.group(1))} {text}" if heading else text
                )
        elif local == "tbl":
            rows = []
            for row in child.iter():
                if _local_name(row.tag) == "tr":
                    rows.append(
                        [
                            _element_text(cell)
                            for cell in row
                            if _local_name(cell.tag) == "tc"
                        ]
                    )
            table = _markdown_table(rows)
            if table:
                blocks.append(table)
    return [_page(source, 1, "\n\n".join(blocks), location={"section": "document"})]


def _parse_pptx(path: str) -> list[dict[str, Any]]:
    source = os.path.basename(path)

    def slide_number(name: str) -> int:
        match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
        if match is None:  # guarded by the archive-member filter below
            raise SourceParseError("invalid PowerPoint slide member")
        return int(match.group(1))

    with _safe_archive(path) as archive:
        fallback_names = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=slide_number,
        )
        names = fallback_names
        presentation_part = "ppt/presentation.xml"
        relationships_part = "ppt/_rels/presentation.xml.rels"
        package_names = set(archive.namelist())
        if presentation_part in package_names or relationships_part in package_names:
            if not {
                presentation_part,
                relationships_part,
            }.issubset(package_names):
                raise SourceParseError(
                    "PowerPoint presentation relationships are incomplete"
                )
            presentation = _xml_part(archive, presentation_part)
            relationships = _relationship_targets(archive, relationships_part)
            ordered_names: list[str] = []
            for slide in (
                node
                for node in presentation.iter()
                if _local_name(node.tag) == "sldId"
            ):
                relationship_id = next(
                    (
                        str(value)
                        for key, value in slide.attrib.items()
                        if _local_name(key) == "id" and str(value) in relationships
                    ),
                    "",
                )
                target = relationships.get(relationship_id, "")
                target_path = PurePosixPath(target)
                if (
                    not relationship_id
                    or not target
                    or target.startswith(("/", "\\"))
                    or ".." in target_path.parts
                ):
                    raise UnsafeSourceArchiveError(
                        "slide relationship escapes its package directory"
                    )
                part = str(PurePosixPath("ppt") / target_path)
                if (
                    not re.fullmatch(r"ppt/slides/slide\d+\.xml", part)
                    or part not in package_names
                    or part in ordered_names
                ):
                    raise SourceParseError(
                        "PowerPoint slide relationship targets an invalid part"
                    )
                ordered_names.append(part)
            # Once presentation metadata exists it is authoritative even for
            # an empty deck. Falling back to every slide*.xml part would index
            # orphan/deleted slide content that is not part of the presentation.
            names = ordered_names
        pages = []
        for index, name in enumerate(names, 1):
            root = _xml_part(archive, name)
            table_text_nodes: set[int] = set()
            tables: list[str] = []
            for table in (
                node for node in root.iter() if _local_name(node.tag) == "tbl"
            ):
                rows = []
                for row in table:
                    if _local_name(row.tag) != "tr":
                        continue
                    cells = []
                    for cell in row:
                        if _local_name(cell.tag) != "tc":
                            continue
                        cells.append(_element_text(cell))
                        table_text_nodes.update(
                            id(node)
                            for node in cell.iter()
                            if _local_name(node.tag) == "t"
                        )
                    rows.append(cells)
                rendered = _markdown_table(rows)
                if rendered:
                    tables.append(rendered)
            texts = [
                node.text or ""
                for node in root.iter()
                if _local_name(node.tag) == "t" and id(node) not in table_text_nodes
            ]
            content = "\n".join(text.strip() for text in texts if text.strip())
            if tables:
                content += ("\n\n" if content else "") + "\n\n".join(tables)
            pages.append(
                _page(
                    source,
                    index,
                    content,
                    location={"slide": index},
                )
            )
    return pages


def _relationship_targets(archive: zipfile.ZipFile, name: str) -> dict[str, str]:
    root = _xml_part(archive, name)
    return {
        str(node.attrib.get("Id")): str(node.attrib.get("Target"))
        for node in root
        if node.attrib.get("Id") and node.attrib.get("Target")
    }


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml_part(archive, "xl/sharedStrings.xml")
    return [_element_text(node) for node in root if _local_name(node.tag) == "si"]


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    value = next((node.text or "" for node in cell if _local_name(node.tag) == "v"), "")
    if kind == "s" and value.isdigit() and int(value) < len(shared):
        return shared[int(value)]
    if kind == "inlineStr":
        return _element_text(cell)
    return value


def _parse_xlsx(path: str) -> list[dict[str, Any]]:
    source = os.path.basename(path)
    with _safe_archive(path) as archive:
        workbook = _xml_part(archive, "xl/workbook.xml")
        relationships = _relationship_targets(archive, "xl/_rels/workbook.xml.rels")
        shared = _shared_strings(archive)
        pages = []
        for index, sheet in enumerate(
            (node for node in workbook.iter() if _local_name(node.tag) == "sheet"), 1
        ):
            rel_id = next(
                (
                    value
                    for key, value in sheet.attrib.items()
                    if _local_name(key) == "id"
                ),
                "",
            )
            target = relationships.get(rel_id, "")
            if (
                not target
                or target.startswith(("/", "\\"))
                or ".." in PurePosixPath(target).parts
            ):
                raise UnsafeSourceArchiveError(
                    "worksheet relationship escapes its package directory"
                )
            part = str(PurePosixPath("xl") / target)
            if not part.startswith("xl/worksheets/"):
                raise UnsafeSourceArchiveError(
                    "worksheet relationship targets an invalid package part"
                )
            root = _xml_part(archive, part)
            rows = []
            for row in root.iter():
                if _local_name(row.tag) == "row":
                    values: list[str] = []
                    for cell in row:
                        if _local_name(cell.tag) != "c":
                            continue
                        reference = str(cell.attrib.get("r") or "")
                        column_letters = re.match(r"[A-Za-z]+", reference)
                        if column_letters:
                            column = 0
                            for char in column_letters.group(0).upper():
                                column = column * 26 + ord(char) - ord("A") + 1
                            values.extend([""] * max(0, column - len(values) - 1))
                        values.append(_cell_value(cell, shared))
                    rows.append(values)
            name = str(sheet.attrib.get("name") or f"Sheet{index}")
            pages.append(
                _page(
                    source,
                    index,
                    f"## {name}\n\n{_markdown_table(rows)}",
                    location={"sheet": name},
                )
            )
    return pages


def _parse_html(path: str) -> list[dict[str, Any]]:
    parser = _SemanticHtmlParser()
    parser.feed(_decode_text(_read_bounded(path)))
    parser.close()
    return [
        _page(
            os.path.basename(path), 1, parser.text(), location={"section": "document"}
        )
    ]


def _parse_text(path: str) -> list[dict[str, Any]]:
    text = _decode_text(_read_bounded(path)).replace("\r\n", "\n").replace("\r", "\n")
    return [
        _page(
            os.path.basename(path),
            1,
            text,
            location={"line_start": 1, "line_end": max(1, text.count("\n") + 1)},
        )
    ]


def _parse_image(
    path: str,
    *,
    settings: Settings | None = None,
    runner=subprocess.run,
) -> list[dict[str, Any]]:
    config = OcrConfig.from_settings(settings or get_settings())
    if not config.enabled:
        if config.required:
            raise OcrUnavailableError("OCR is required but disabled")
        return [
            _page(
                os.path.basename(path),
                1,
                "",
                extraction_method="none",
                ocr_status="disabled",
                is_ocr_fallback=True,
                location={"image": 1},
            )
        ]
    dependency = probe_ocr_dependency(config)
    if not dependency.available:
        if config.required:
            raise OcrUnavailableError("configured OCR binary is unavailable")
        return [
            _page(
                os.path.basename(path),
                1,
                "",
                extraction_method="none",
                ocr_status="unavailable",
                is_ocr_fallback=True,
                location={"image": 1},
            )
        ]
    raw = _read_bounded(path)
    try:
        completed = runner(
            [
                config.binary,
                "stdin",
                "stdout",
                "-l",
                config.languages,
                "--dpi",
                str(config.dpi),
            ],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.page_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if config.required:
            raise OcrTimeoutError("OCR image timeout exceeded") from exc
        return [
            _page(
                os.path.basename(path),
                1,
                "",
                extraction_method="none",
                ocr_status="timeout",
                is_ocr_fallback=True,
                location={"image": 1},
            )
        ]
    except OSError as exc:
        if config.required:
            raise OcrUnavailableError(
                "configured OCR binary became unavailable"
            ) from exc
        return [
            _page(
                os.path.basename(path),
                1,
                "",
                extraction_method="none",
                ocr_status="unavailable",
                is_ocr_fallback=True,
                location={"image": 1},
            )
        ]
    if completed.returncode != 0:
        if config.required:
            raise OcrExecutionError(
                f"OCR process exited with status {completed.returncode}"
            )
        return [
            _page(
                os.path.basename(path),
                1,
                "",
                extraction_method="none",
                ocr_status="failed",
                is_ocr_fallback=True,
                location={"image": 1},
            )
        ]
    output = (
        completed.stdout
        if isinstance(completed.stdout, bytes)
        else completed.stdout.encode("utf-8")
    )
    if len(output) > MAX_OCR_OUTPUT_BYTES:
        raise OcrExecutionError("OCR image output exceeds the safety limit")
    text = normalize_ocr_text(output.decode("utf-8", errors="replace"))
    return [
        _page(
            os.path.basename(path),
            1,
            text,
            extraction_method="ocr",
            ocr_status="succeeded",
            ocr_provider=config.provider,
            is_ocr_fallback=True,
            location={"image": 1},
        )
    ]


def parse_source(
    path: str,
    *,
    source_document: SourceDocument | None = None,
    settings: Settings | None = None,
    ocr_engine: Any | None = None,
    image_ocr_runner=subprocess.run,
) -> list[ParsedPage]:
    extension = Path(path).suffix.casefold()
    if extension not in SUPPORTED_EXTENSIONS:
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        raise UnsupportedSourceError(f"unsupported source type: {media_type}")
    pages: list[dict[str, Any]]
    if extension == ".pdf":
        pages = [
            dict(page)
            for page in smart_parse(path, settings=settings, ocr_engine=ocr_engine)
        ]
    elif extension in {".md", ".markdown", ".txt"}:
        pages = _parse_text(path)
    elif extension in {".html", ".htm"}:
        pages = _parse_html(path)
    elif extension == ".docx":
        pages = _parse_docx(path)
    elif extension == ".pptx":
        pages = _parse_pptx(path)
    elif extension == ".xlsx":
        pages = _parse_xlsx(path)
    else:
        pages = _parse_image(path, settings=settings, runner=image_ocr_runner)
    return cast(list[ParsedPage], annotate_parsed_pages(pages, source_document))
