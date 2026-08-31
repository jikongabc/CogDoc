import pytest
from tests._native import require_rust_core

rust_core = require_rust_core(
    "list_supported_files_native",
    "scan_pdf_manifest_native",
    "scan_source_manifest_native",
)


# 写入。
def _write(path, content: bytes = b"%PDF-1.4 dummy"):
    path.write_bytes(content)


# 构造或驱动 names 测试场景。
def _names(manifest):
    return [d["name"] for d in manifest["documents"]]


# 验证 only pdf files are scanned 场景。
def test_only_pdf_files_are_scanned(tmp_path):
    # 验证扫描只收录 PDF 文件。
    _write(tmp_path / "a.pdf")
    _write(tmp_path / "notes.txt")
    _write(tmp_path / "image.png")

    manifest = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))
    assert _names(manifest) == ["a.pdf"]


# 验证 uppercase extension is included 场景。
def test_uppercase_extension_is_included(tmp_path):
    # 验证 PDF 后缀大小写不敏感。
    _write(tmp_path / "lower.pdf")
    _write(tmp_path / "UPPER.PDF")

    manifest = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))
    assert _names(manifest) == ["UPPER.PDF", "lower.pdf"]


# 验证 documents are sorted by name 场景。
def test_documents_are_sorted_by_name(tmp_path):
    # 验证 manifest 文档列表按文件名稳定排序。
    for name in ["c.pdf", "a.pdf", "b.pdf"]:
        _write(tmp_path / name)

    manifest = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))
    assert _names(manifest) == ["a.pdf", "b.pdf", "c.pdf"]


# 验证 manifest carries fingerprint fields 场景。
def test_manifest_carries_fingerprint_fields(tmp_path):
    # 验证 manifest 返回文件名、大小、哈希与知识库 ID。
    _write(tmp_path / "a.pdf", b"hello world")
    manifest = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))

    doc = manifest["documents"][0]
    assert doc["name"] == "a.pdf"
    assert doc["size"] == len(b"hello world")
    assert len(doc["sha256"]) == 64
    assert manifest["doc_id"] == "kb"


# 验证 content change triggers hash change 场景。
def test_content_change_triggers_hash_change(tmp_path):
    # 验证 PDF 内容变化会改变指纹并触发 stale。
    pdf = tmp_path / "a.pdf"
    _write(pdf, b"version one")
    before = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))["documents"][0]

    _write(pdf, b"version two -- changed")
    after = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))["documents"][0]

    assert before["sha256"] != after["sha256"]
    assert before != after


# 验证 identical content is stable across scans 场景。
def test_identical_content_is_stable_across_scans(tmp_path):
    # 验证相同内容重复扫描得到相同 documents 列表。
    _write(tmp_path / "a.pdf", b"stable bytes")
    first = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))
    second = rust_core.scan_pdf_manifest_native("kb", str(tmp_path))
    assert first["documents"] == second["documents"]


# 验证 missing directory raises 场景。
def test_missing_directory_raises(tmp_path):
    # 验证目录不存在时 native scanner 抛出异常。
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        rust_core.scan_pdf_manifest_native("kb", str(missing))


def test_pdf_manifest_rejects_non_directory_with_specific_error(tmp_path):
    regular_file = tmp_path / "regular.pdf"
    _write(regular_file)

    with pytest.raises(NotADirectoryError):
        rust_core.scan_pdf_manifest_native("kb", str(regular_file))


def test_source_manifest_scans_allowlist_and_enforces_per_file_limit(tmp_path):
    _write(tmp_path / "a.md", b"abc")
    _write(tmp_path / "b.TXT", b"1234")
    _write(tmp_path / "ignored.pdf", b"pdf")

    manifest = rust_core.scan_source_manifest_native(
        "kb", str(tmp_path), [".md", ".txt"], 4
    )

    assert _names(manifest) == ["a.md", "b.TXT"]
    assert [row["size"] for row in manifest["documents"]] == [3, 4]
    assert rust_core.list_supported_files_native(
        str(tmp_path), [".md", ".txt"]
    ) == ["a.md", "b.TXT"]

    with pytest.raises(OSError, match=r"source exceeds 2 bytes: a\.md"):
        rust_core.scan_source_manifest_native(
            "kb", str(tmp_path), [".md"], 2
        )


def test_source_manifest_preserves_missing_and_not_directory_errors(tmp_path):
    missing = tmp_path / "missing"
    regular_file = tmp_path / "regular.txt"
    regular_file.write_text("content", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        rust_core.scan_source_manifest_native("kb", str(missing), [".txt"], 10)
    with pytest.raises(NotADirectoryError):
        rust_core.scan_source_manifest_native(
            "kb", str(regular_file), [".txt"], 10
        )
