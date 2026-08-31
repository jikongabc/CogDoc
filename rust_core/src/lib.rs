use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

mod bm25;
mod citation;
mod evidence_span;
mod rrf;
mod scanner;
mod tokenizer;

// 扫描 PDF 目录生成指纹清单，供 Python 端校验本地索引是否过期
#[pyfunction]
fn scan_pdf_manifest_native<'py>(
    py: Python<'py>,
    doc_id: String,
    doc_dir: String,
) -> PyResult<Bound<'py, PyDict>> {
    let scanned_files = match scanner::parallel_scan_manifest(&doc_dir) {
        Ok(files) => files,
        Err(error) => return Err(PyErr::from(error)),
    };

    let py_list = PyList::empty(py);
    for file in scanned_files {
        let single_file_dict = PyDict::new(py);
        single_file_dict.set_item("name", file.name)?;
        single_file_dict.set_item("size", file.size)?;
        single_file_dict.set_item("sha256", file.sha256)?;
        py_list.append(single_file_dict)?;
    }

    // 组装最终清单；doc_dir 为机器相关路径，Python 端比对时会忽略
    let final_manifest = PyDict::new(py);
    final_manifest.set_item("doc_id", doc_id)?;
    final_manifest.set_item("doc_dir", doc_dir)?;
    final_manifest.set_item("documents", py_list)?;

    Ok(final_manifest)
}

// 与通用 manifest 扫描复用同一份格式白名单和目录语义，只返回稳定文件名。
#[pyfunction]
fn list_supported_files_native(
    source_dir: String,
    supported_extensions: Vec<String>,
) -> PyResult<Vec<String>> {
    scanner::list_supported_source_files(&source_dir, &supported_extensions).map_err(PyErr::from)
}

// 扫描所有受支持来源格式；扩展名与单文件上限由 Python 产品契约传入。
#[pyfunction]
fn scan_source_manifest_native<'py>(
    py: Python<'py>,
    doc_id: String,
    doc_dir: String,
    supported_extensions: Vec<String>,
    max_source_bytes: u64,
) -> PyResult<Bound<'py, PyDict>> {
    let scanned_files =
        scanner::parallel_scan_source_manifest(&doc_dir, &supported_extensions, max_source_bytes)
            .map_err(PyErr::from)?;

    let py_list = PyList::empty(py);
    for file in scanned_files {
        let single_file_dict = PyDict::new(py);
        single_file_dict.set_item("name", file.name)?;
        single_file_dict.set_item("size", file.size)?;
        single_file_dict.set_item("sha256", file.sha256)?;
        py_list.append(single_file_dict)?;
    }

    let final_manifest = PyDict::new(py);
    final_manifest.set_item("doc_id", doc_id)?;
    final_manifest.set_item("doc_dir", doc_dir)?;
    final_manifest.set_item("documents", py_list)?;
    Ok(final_manifest)
}

// 对向量召回与 BM25 召回结果做 RRF 融合，返回前 top_n 文档
#[pyfunction]
fn rrf_fusion_native<'py>(
    vector_docs: Vec<Bound<'py, PyDict>>,
    bm25_docs: Vec<Bound<'py, PyDict>>,
    k: f64,
    top_n: usize,
) -> PyResult<Vec<Bound<'py, PyDict>>> {
    rrf::rrf_fusion_core(vector_docs, bm25_docs, k, top_n)
}

// 校验回答里的引用标签是否只引用了本轮召回上下文中的文件和页码
#[pyfunction]
fn validate_citations_native<'py>(
    py: Python<'py>,
    answer: String,
    valid_docs: Vec<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>> {
    citation::validate_citations_core(py, answer, valid_docs)
}

// 中英文混合分词，供 BM25 索引/检索与摘要章节选择共用
#[pyfunction]
fn tokenize_mixed_text_native(text: String) -> Vec<String> {
    tokenizer::tokenize_mixed_text_core(&text)
}

// 整批分词，rayon 并行后单次跨界返回，供索引入库摊薄逐 chunk 调用
#[pyfunction]
fn tokenize_corpus_native(texts: Vec<String>) -> Vec<Vec<String>> {
    tokenizer::tokenize_corpus_core(texts)
}

// 在一次 native 调用内完成分句、分词、候选窗口评分与稳定决胜。
#[pyfunction]
fn select_evidence_span_native<'py>(
    py: Python<'py>,
    text: String,
    query_terms: Vec<String>,
    requirement_terms: Vec<Vec<String>>,
    target_terms: Vec<String>,
    max_chars: usize,
    context_sentences: usize,
) -> PyResult<Bound<'py, PyDict>> {
    if max_chars == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "max_chars must be positive",
        ));
    }
    let selection = evidence_span::select_evidence_span_core(
        &text,
        &query_terms,
        &requirement_terms,
        &target_terms,
        max_chars,
        context_sentences,
    );
    let result = PyDict::new(py);
    result.set_item("start", selection.start)?;
    result.set_item("end", selection.end)?;
    result.set_item("score", selection.score)?;
    result.set_item("matched_terms", selection.matched_terms)?;
    result.set_item("reason", selection.reason)?;
    result.set_item("fallback", selection.fallback)?;
    Ok(result)
}

// 模块入口：向 Python 注册导出的 native 函数
#[pymodule]
fn rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(scan_pdf_manifest_native, m)?)?;
    m.add_function(wrap_pyfunction!(list_supported_files_native, m)?)?;
    m.add_function(wrap_pyfunction!(scan_source_manifest_native, m)?)?;
    m.add_function(wrap_pyfunction!(rrf_fusion_native, m)?)?;
    m.add_function(wrap_pyfunction!(validate_citations_native, m)?)?;
    m.add_function(wrap_pyfunction!(tokenize_mixed_text_native, m)?)?;
    m.add_function(wrap_pyfunction!(tokenize_corpus_native, m)?)?;
    m.add_function(wrap_pyfunction!(select_evidence_span_native, m)?)?;
    m.add_class::<bm25::Bm25Index>()?;
    Ok(())
}
