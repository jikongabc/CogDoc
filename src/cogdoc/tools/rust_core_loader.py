import importlib
from types import ModuleType


# 运行链路里各处 ensure_rust_core 调用的符号并集；check_native 与就绪探针共用。
REQUIRED_NATIVE_SYMBOLS = (
    "scan_pdf_manifest_native",
    "list_supported_files_native",
    "scan_source_manifest_native",
    "rrf_fusion_native",
    "validate_citations_native",
    "tokenize_mixed_text_native",
    "tokenize_corpus_native",
    "select_evidence_span_native",
    "Bm25Index",
)


# 确保 rust core。
def ensure_rust_core(*required: str) -> ModuleType:
    # 统一校验 Rust 扩展是否已安装且包含必需符号。
    try:
        rust_core = importlib.import_module("rust_core")
    except ImportError as exc:
        raise RuntimeError(
            "Rust 扩展 rust_core 未安装。请先运行: cd rust_core && maturin develop"
        ) from exc

    missing = [name for name in required if not hasattr(rust_core, name)]
    if missing:
        missing_str = ", ".join(missing)
        raise RuntimeError(
            f"Rust 扩展 rust_core 未正确加载，缺少: {missing_str}。请先运行: cd rust_core && maturin develop"
        )

    return rust_core
