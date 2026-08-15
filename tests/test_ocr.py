from types import SimpleNamespace

import pytest

from cogdoc.config.settings import Settings
from cogdoc.tools import parser
from cogdoc.tools.ocr import (
    OcrConfig,
    OcrPageLimitError,
    OcrPageResult,
    OcrTimeoutError,
    TesseractOcrEngine,
    normalize_ocr_text,
    probe_ocr_dependency,
)


class FakePage:
    def __init__(self, text="", blocks=None):
        self.text = text
        self.blocks = blocks or []
        self.rect = SimpleNamespace(width=600)

    def get_text(self, mode=None):
        return self.blocks if mode == "blocks" else self.text

    def get_pixmap(self, **kwargs):
        return SimpleNamespace(tobytes=lambda kind: b"png")


class FakeDocument:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def extract(self, page):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


def _settings(**overrides):
    values = {
        "cogdoc_ocr_enabled": True,
        "cogdoc_ocr_required": False,
        "cogdoc_ocr_max_pages": 100,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_normalize_ocr_text_removes_form_feed_and_collapses_spacing():
    assert normalize_ocr_text("Ａ  B\r\n\r\n\r\n中文\x0c") == "A B\n\n中文"


def test_tesseract_engine_uses_stdin_without_shell(monkeypatch):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b" OCR text\n", stderr=b"")

    monkeypatch.setattr("cogdoc.tools.ocr.shutil.which", lambda binary: "/usr/bin/tesseract")
    engine = TesseractOcrEngine(OcrConfig(enabled=True), runner=runner)

    result = engine.extract(FakePage())

    assert result.text == "OCR text"
    assert calls[0][0][:3] == ["tesseract", "stdin", "stdout"]
    assert "shell" not in calls[0][1]
    assert calls[0][1]["input"] == b"png"


def test_probe_reports_missing_binary():
    status = probe_ocr_dependency(OcrConfig(enabled=True), which=lambda binary: None)
    assert status.available is False
    assert status.reason == "binary_not_found"


def test_smart_parse_ocr_only_for_candidate_pages(monkeypatch):
    native_blocks = [(0, 0, 500, 50, "native text " * 8)]
    document = FakeDocument([FakePage("native text " * 8, native_blocks), FakePage()])
    monkeypatch.setattr(parser.fitz, "open", lambda path: document)
    engine = FakeEngine([OcrPageResult("扫描页内容", "tesseract")])

    pages = parser.smart_parse("mixed.pdf", settings=_settings(), ocr_engine=engine)

    assert engine.calls == 1
    assert pages[0]["extraction_method"] == "native"
    assert pages[1]["text"] == "扫描页内容"
    assert pages[1]["extraction_method"] == "ocr"
    assert pages[1]["ocr_status"] == "succeeded"
    assert document.closed is True


def test_optional_ocr_timeout_degrades_but_required_mode_fails(monkeypatch):
    monkeypatch.setattr(parser.fitz, "open", lambda path: FakeDocument([FakePage()]))
    optional = FakeEngine([OcrTimeoutError("timeout")])
    pages = parser.smart_parse("scan.pdf", settings=_settings(), ocr_engine=optional)
    assert pages[0]["text"] == ""
    assert pages[0]["ocr_status"] == "timeout"

    monkeypatch.setattr(parser.fitz, "open", lambda path: FakeDocument([FakePage()]))
    required = FakeEngine([OcrTimeoutError("timeout")])
    with pytest.raises(OcrTimeoutError):
        parser.smart_parse(
            "scan.pdf",
            settings=_settings(cogdoc_ocr_required=True),
            ocr_engine=required,
        )


def test_ocr_page_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(
        parser.fitz, "open", lambda path: FakeDocument([FakePage(), FakePage()])
    )
    engine = FakeEngine([OcrPageResult("first", "tesseract")])
    pages = parser.smart_parse(
        "scan.pdf",
        settings=_settings(cogdoc_ocr_max_pages=1),
        ocr_engine=engine,
    )
    assert engine.calls == 1
    assert pages[0]["ocr_status"] == "succeeded"
    assert pages[1]["ocr_status"] == "limit"

    monkeypatch.setattr(
        parser.fitz, "open", lambda path: FakeDocument([FakePage(), FakePage()])
    )
    with pytest.raises(OcrPageLimitError):
        parser.smart_parse(
            "scan.pdf",
            settings=_settings(cogdoc_ocr_max_pages=1, cogdoc_ocr_required=True),
            ocr_engine=FakeEngine([OcrPageResult("first", "tesseract")]),
        )


def test_ocr_disabled_preserves_previous_empty_scan_page_behavior(monkeypatch):
    monkeypatch.setattr(parser.fitz, "open", lambda path: FakeDocument([FakePage()]))
    engine = FakeEngine([])
    pages = parser.smart_parse(
        "scan.pdf",
        settings=_settings(cogdoc_ocr_enabled=False),
        ocr_engine=engine,
    )
    assert engine.calls == 0
    assert pages[0]["text"] == ""
    assert pages[0]["ocr_status"] == "disabled"


def test_ocr_disabled_preserves_short_native_text(monkeypatch):
    short_text = "截止日期：8月31日"
    monkeypatch.setattr(
        parser.fitz,
        "open",
        lambda path: FakeDocument([FakePage(short_text, [(0, 0, 200, 20, short_text)])]),
    )

    pages = parser.smart_parse(
        "short-native.pdf",
        settings=_settings(cogdoc_ocr_enabled=False),
    )

    assert pages[0]["text"] == short_text
    assert pages[0]["extraction_method"] == "native"
    assert pages[0]["ocr_status"] == "disabled"
