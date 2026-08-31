use rayon::prelude::*;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

// 分块读取文件计算 SHA256，避免大文件一次性占满内存
pub fn calculate_sha256<P: AsRef<Path>>(path: P) -> std::io::Result<String> {
    calculate_sha256_bounded(path, u64::MAX).map(|(digest, _)| digest)
}

fn calculate_sha256_bounded<P: AsRef<Path>>(
    path: P,
    max_bytes: u64,
) -> std::io::Result<(String, u64)> {
    let file = File::open(path)?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 1024 * 1024];

    let mut size = 0_u64;
    loop {
        let bytes_read = reader.read(&mut buffer)?;
        if bytes_read == 0 {
            break; // 读到文件末尾
        }
        size = size.saturating_add(bytes_read as u64);
        if size > max_bytes {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("source exceeds {max_bytes} bytes"),
            ));
        }
        hasher.update(&buffer[..bytes_read]); // 仅喂入本次实际读到的字节
    }

    Ok((hex::encode(hasher.finalize()), size))
}

fn supported_source_paths(
    doc_dir: &str,
    supported_extensions: &[String],
) -> std::io::Result<Vec<PathBuf>> {
    let dir_path = Path::new(doc_dir);
    if !dir_path.exists() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "目标目录不存在",
        ));
    }
    if !dir_path.is_dir() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotADirectory,
            "目标路径不是目录",
        ));
    }
    let extensions: HashSet<String> = supported_extensions
        .iter()
        .map(|value| value.trim_start_matches('.').to_lowercase())
        .filter(|value| !value.is_empty())
        .collect();
    let mut paths = Vec::new();
    for entry in std::fs::read_dir(dir_path)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_file()
            && path.extension().is_some_and(|extension| {
                extensions.contains(&extension.to_string_lossy().to_lowercase())
            })
        {
            paths.push(path);
        }
    }
    paths.sort();
    Ok(paths)
}

pub fn list_supported_source_files(
    doc_dir: &str,
    supported_extensions: &[String],
) -> std::io::Result<Vec<String>> {
    supported_source_paths(doc_dir, supported_extensions).map(|paths| {
        paths
            .into_iter()
            .map(|path| {
                path.file_name()
                    .expect("read_dir paths always have a file name")
                    .to_string_lossy()
                    .into_owned()
            })
            .collect()
    })
}

// 按调用方提供的格式白名单扫描并行计算指纹，同时保持文件名稳定排序。
pub fn parallel_scan_source_manifest(
    doc_dir: &str,
    supported_extensions: &[String],
    max_source_bytes: u64,
) -> std::io::Result<Vec<ScannedFileInfo>> {
    let paths = supported_source_paths(doc_dir, supported_extensions)?;

    let results: Vec<std::io::Result<(usize, ScannedFileInfo)>> = paths
        .par_iter()
        .enumerate()
        .map(|(index, path)| {
            let name = path
                .file_name()
                .expect("read_dir paths always have a file name")
                .to_string_lossy()
                .into_owned();
            let (sha256, size) =
                calculate_sha256_bounded(path, max_source_bytes).map_err(|error| {
                    if error.kind() == std::io::ErrorKind::InvalidData {
                        std::io::Error::new(
                            error.kind(),
                            format!("source exceeds {max_source_bytes} bytes: {name}"),
                        )
                    } else {
                        error
                    }
                })?;
            Ok((index, ScannedFileInfo { name, size, sha256 }))
        })
        .collect();
    let mut valid_files = Vec::with_capacity(results.len());
    for result in results {
        valid_files.push(result?);
    }
    valid_files.sort_by_key(|(index, _)| *index);
    Ok(valid_files.into_iter().map(|(_, file)| file).collect())
}

// 单个 PDF 的指纹信息(文件名/大小/内容哈希)
pub struct ScannedFileInfo {
    pub name: String,
    pub size: u64,
    pub sha256: String,
}

// 扫描目录下所有 PDF，并行计算指纹，结果按文件名稳定排序返回
pub fn parallel_scan_manifest(doc_dir: &str) -> std::io::Result<Vec<ScannedFileInfo>> {
    let pdf_paths = supported_source_paths(doc_dir, &[".pdf".to_string()])?;

    // 并行计算各文件哈希，保留下标用于还原排序
    let results: Vec<std::io::Result<(usize, ScannedFileInfo)>> = pdf_paths
        .par_iter()
        .enumerate()
        .map(|(idx, path)| {
            let metadata = std::fs::metadata(path)?;
            let file_name = path.file_name().unwrap().to_string_lossy().into_owned(); // read_dir 产出的文件路径必含文件名
            let file_size = metadata.len();
            let hash_str = calculate_sha256(path)?; // 任一文件哈希失败则整次扫描失败

            Ok((
                idx,
                ScannedFileInfo {
                    name: file_name,
                    size: file_size,
                    sha256: hash_str,
                },
            ))
        })
        .collect();

    let mut valid_files = Vec::with_capacity(results.len());
    for res in results {
        valid_files.push(res?); // 向上抛出任一并行任务的 IO 错误
    }
    valid_files.sort_by_key(|(idx, _)| *idx); // 还原并行前的文件名顺序

    Ok(valid_files.into_iter().map(|(_, file)| file).collect())
}
