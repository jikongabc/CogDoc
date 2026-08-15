import pymupdf as fitz  # 直接导入 pymupdf，避免被 PyPI 同名 stub 包 fitz 顶替
import re
import os
from typing import Any, List
from cogdoc.config.settings import Settings, get_settings
from cogdoc.graph.state import ParsedPage
from cogdoc.tools.ocr import (
    OcrConfig,
    OcrError,
    OcrExecutionError,
    OcrPageLimitError,
    OcrTimeoutError,
    OcrUnavailableError,
    TesseractOcrEngine,
    ocr_config_signature,
)


# 解析逻辑变化时 bump：进入增量复用门控，避免未变文档复用旧解析结果。
PARSER_VERSION = (
    "pymupdf_smart_parse_v3_ocr_" + ocr_config_signature(get_settings())
)


def _ordered_native_text(page: Any, blocks: list) -> str:
    width = page.rect.width
    center_xs = [(b[0] + b[2]) / 2 for b in blocks if len(b[4].strip()) > 5]
    left_count = sum(1 for cx in center_xs if cx < width * 0.4)
    right_count = sum(1 for cx in center_xs if cx > width * 0.6)
    if left_count > 3 and right_count > 3:
        left_blocks = sorted(
            [b for b in blocks if (b[0] + b[2]) / 2 <= width * 0.5],
            key=lambda x: x[1],
        )
        right_blocks = sorted(
            [b for b in blocks if (b[0] + b[2]) / 2 > width * 0.5],
            key=lambda x: x[1],
        )
        page_text = "\n".join(b[4] for b in left_blocks)
        page_text += "\n" + "\n".join(b[4] for b in right_blocks)
    else:
        ordered = sorted(blocks, key=lambda x: (x[1], x[0]))
        page_text = "\n".join(b[4] for b in ordered)
    return re.sub(r"\n{3,}", "\n\n", page_text).strip()


def _ocr_failure_page(
    *, page_num: int, source_name: str, native_text: str, config: OcrConfig, status: str
) -> ParsedPage:
    return {
        "page": page_num,
        "source": source_name,
        "text": native_text,
        "is_ocr_fallback": True,
        "extraction_method": "native" if native_text else "none",
        "ocr_status": status,
        "ocr_provider": config.provider,
    }


# 完成 smartparse 处理。
def smart_parse(
    pdf_path: str,
    *,
    settings: Settings | None = None,
    ocr_engine: Any | None = None,
) -> List[ParsedPage]:
    source_name = os.path.basename(pdf_path)  # 文件名
    doc = fitz.open(pdf_path)
    parsed_pages: List[ParsedPage] = []
    resolved_settings = settings or get_settings()
    config = OcrConfig.from_settings(resolved_settings)
    engine = ocr_engine or (TesseractOcrEngine(config) if config.enabled else None)
    ocr_attempts = 0

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_num = page_idx + 1
            native_text = page.get_text().strip()
            blocks = page.get_text("blocks")
            threshold = config.min_native_chars if config.enabled else 20
            is_candidate = len(native_text) < threshold and len(blocks) <= 1

            if not is_candidate:
                parsed_pages.append(
                    {
                        "page": page_num,
                        "source": source_name,
                        "text": _ordered_native_text(page, blocks),
                        "is_ocr_fallback": False,
                        "extraction_method": "native",
                        "ocr_status": "not_needed",
                    }
                )
                continue

            if not config.enabled:
                parsed_pages.append(
                    _ocr_failure_page(
                        page_num=page_num,
                        source_name=source_name,
                        # OCR being disabled must not discard text that PyMuPDF
                        # already extracted.  Short pages often contain exactly
                        # the dates, identifiers, or contact details a RAG query
                        # needs, even though they also satisfy the OCR-candidate
                        # heuristic.
                        native_text=native_text,
                        config=config,
                        status="disabled",
                    )
                )
                continue

            if ocr_attempts >= config.max_pages:
                error = OcrPageLimitError("OCR page limit exceeded")
                if config.required:
                    raise error
                parsed_pages.append(
                    _ocr_failure_page(
                        page_num=page_num,
                        source_name=source_name,
                        native_text=native_text,
                        config=config,
                        status="limit",
                    )
                )
                continue

            ocr_attempts += 1
            try:
                result = engine.extract(page)
                if not result.text:
                    raise OcrExecutionError("OCR returned empty text")
                parsed_pages.append(
                    {
                        "page": page_num,
                        "source": source_name,
                        "text": result.text,
                        "is_ocr_fallback": True,
                        "extraction_method": "ocr",
                        "ocr_status": "succeeded",
                        "ocr_provider": result.provider,
                    }
                )
            except OcrError as exc:
                if config.required:
                    raise
                if isinstance(exc, OcrTimeoutError):
                    status = "timeout"
                elif isinstance(exc, OcrUnavailableError):
                    status = "unavailable"
                else:
                    status = "failed"
                parsed_pages.append(
                    _ocr_failure_page(
                        page_num=page_num,
                        source_name=source_name,
                        native_text=native_text,
                        config=config,
                        status=status,
                    )
                )
    finally:
        doc.close()
    return parsed_pages
