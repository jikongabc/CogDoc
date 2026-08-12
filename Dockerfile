# ---- 构建阶段：在固定 manylinux/maturin 镜像中编译原生 wheel ----
FROM ghcr.io/pyo3/maturin:v1.14.1@sha256:2665227312dd1eab1c29c70a001dc8aac53155a2d048bede3b2df7f1691c8e38 AS rust-builder

ARG RUST_TOOLCHAIN=1.97.1
RUN rustup toolchain install "${RUST_TOOLCHAIN}" --profile minimal \
    && rustup default "${RUST_TOOLCHAIN}" \
    && rustc --version

WORKDIR /build
COPY rust_core/ ./rust_core/
RUN cd rust_core && maturin build --release --locked \
    --interpreter /opt/python/cp313-cp313/bin/python --out /wheels

# ---- 运行阶段 ----
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS runtime

# libgomp1 是 torch / sentence-transformers 的 OpenMP 运行时依赖。
# OCR 默认关闭；启用时使用本地 Tesseract CLI，镜像预装英文和简体中文语言数据。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 默认 CPU 部署：先装 CPU 版 torch（独立缓存层），避免镜像塞入数 GB 用不上的 CUDA 包。
ARG PIP_NETWORK_TIMEOUT_SECONDS=120
ARG PIP_NETWORK_RETRIES=10
RUN python -m pip install --no-cache-dir \
        --timeout "${PIP_NETWORK_TIMEOUT_SECONDS}" --retries "${PIP_NETWORK_RETRIES}" \
        "pip==26.0.1" \
    && python -m pip install --no-cache-dir \
        --timeout "${PIP_NETWORK_TIMEOUT_SECONDS}" --retries "${PIP_NETWORK_RETRIES}" \
        torch==2.12.0 --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY scripts/backup_state.py scripts/restore_state.py scripts/migrate_state.py ./scripts/
# 依赖与包元数据统一在 pyproject.toml；torch 已满足版本约束不会重装。
RUN python -m pip install --no-cache-dir \
        --timeout "${PIP_NETWORK_TIMEOUT_SECONDS}" --retries "${PIP_NETWORK_RETRIES}" .
COPY --from=rust-builder /wheels/*.whl /tmp/
RUN python -m pip install --no-cache-dir \
        --timeout "${PIP_NETWORK_TIMEOUT_SECONDS}" --retries "${PIP_NETWORK_RETRIES}" \
        /tmp/*.whl \
    && rm -f /tmp/*.whl

# Installed wheels live under /usr/local, so repository-relative defaults would
# otherwise resolve below a read-only site-packages directory. Keep every
# mutable runtime artifact on the explicitly owned persistent volume. Runtime
# environment variables may still override these image defaults.
ENV COGDOC_DATA_DIR=/app/data \
    COGDOC_BACKUP_DIR=/app/data/backups \
    COGDOC_LOG_FILE=/app/data/logs/cogdoc.jsonl \
    COGDOC_TRACE_DIR=/app/data/logs/traces

# Persisted state is written only below these explicitly owned directories;
# the API process itself never needs root privileges.
RUN groupadd --system cogdoc \
    && useradd --system --gid cogdoc --create-home cogdoc \
    && mkdir -p /app/data/backups /app/logs \
    && chown -R cogdoc:cogdoc /app/data /app/logs
USER cogdoc

# BGE 嵌入/精排模型首次使用时从 HuggingFace 下载；生产可挂载缓存卷或预下载。
# GPU 部署需换 nvidia/cuda 基础镜像并装对应 torch。
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).read()"]
CMD ["python", "-m", "uvicorn", "cogdoc.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "15"]
