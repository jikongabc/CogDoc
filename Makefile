# CogDoc 统一开发命令，覆盖原生扩展构建、健康检查、测试与启动。

PYTHON  ?= python
MATURIN ?= maturin
RUFF    ?= ruff
MYPY    ?= mypy
SHELL   := /bin/bash
SECURITY_TYPECHECK_MODULES := \
	src/cogdoc/api/auth_store.py \
	src/cogdoc/api/audit.py \
	src/cogdoc/api/resource_access.py \
	src/cogdoc/api/tenancy.py \
	src/cogdoc/api/tenant_scope.py \
	src/cogdoc/api/research_access.py
EVAL_SUITE_RELEASE_ARGS := --run-retrieval --retrieval-coverage-profile baseline --rerank --retrieval-gate eval/retrieval_gate.json
RELIABILITY_DIR ?= artifacts/reliability
RELIABILITY_GRACE ?= 10
RELIABILITY_NATIVE_TIMEOUT ?= 900
RELIABILITY_SMOKE_TIMEOUT ?= 120
RELIABILITY_TEST_TIMEOUT ?= 1200
RELIABILITY_EVAL_TIMEOUT ?= 1800
RELIABILITY_SOAK_TIMEOUT ?= 240
RELIABILITY_API_HOST ?= 127.0.0.1
RELIABILITY_API_PORT ?= 8000
RELIABILITY_API_URL ?= http://$(RELIABILITY_API_HOST):$(RELIABILITY_API_PORT)/healthz
RELIABILITY_SOAK_REQUESTS ?= 100
RELIABILITY_SOAK_CONCURRENCY ?= 10
RELIABILITY_REQUEST_TIMEOUT ?= 3
RELIABILITY_MIN_SUCCESS_RATE ?= 0.99
RELIABILITY_MAX_P95_MS ?= 750
UVICORN_GRACEFUL_SHUTDOWN_SECONDS ?= 15

# src-layout：包源码在 src/，入口经 PYTHONPATH 注入，无需先安装即可 run/serve/test。
export PYTHONPATH := src

.PHONY: help install native check lint typecheck-security test smoke-api smoke-account-auth reliability-gate run debug backup eval eval-coverage eval-retrieval-report eval-retrieval-baseline eval-retrieval-gate eval-multi-route calibrate-multi-route eval-quality eval-quality-coverage eval-suite eval-suite-run-retrieval eval-suite-report eval-suite-baseline eval-suite-update-baseline serve frontend

help:
	@echo "make install - 可编辑安装含开发依赖 (pip install -e '.[dev]')"
	@echo "make native  - 构建 rust_core 原生扩展 (cd rust_core && maturin develop --release)"
	@echo "make check   - 校验原生扩展是否就绪 (scripts/check_native.py)"
	@echo "make lint    - 对仓库 Python 代码运行 Ruff（仅豁免旧脚本的路径引导 E402）"
	@echo "make typecheck-security - 对认证、审计、ACL 与租户边界运行定向 mypy"
	@echo "make test    - 运行 pytest 全量测试"
	@echo "make smoke-api - 运行不依赖真实模型/索引的 API E2E smoke"
	@echo "make smoke-account-auth - 启动隔离生产 app，验证账号登录与 Bearer 工作区访问"
	@echo "make reliability-gate - 运行有界 native/smoke/test/eval 与真实 API soak 门禁"
	@echo "make backup  - 备份 data/ 与 logs/traces/ 到 backups/"
	@echo "make eval    - 离线检索评测 recall@k/MRR (scripts/eval_retrieval.py)"
	@echo "make eval-coverage - 只检查检索评测集覆盖面，不执行真实检索"
	@echo "make eval-retrieval-report - 用 100 条真实集运行检索并写入报告"
	@echo "make eval-retrieval-baseline - 生成真实检索基线"
	@echo "make eval-retrieval-gate - 对比真实检索基线并执行绝对门禁"
	@echo "make eval-multi-route - 运行四路单路/leave-one-out 消融评测"
	@echo "make calibrate-multi-route - 从四路报告生成可回滚参数建议"
	@echo "make eval-quality - 离线质量评测 router/citation/faithfulness (scripts/eval_quality.py)"
	@echo "make eval-quality-coverage - 检查质量评测集覆盖面"
	@echo "make eval-suite - 运行组合评测门禁（覆盖审计 + 质量评测）"
	@echo "make eval-suite-run-retrieval - 运行组合评测并执行真实检索"
	@echo "make eval-suite-report - 执行真实检索发布门禁并写入 eval/eval_suite_report.json"
	@echo "make eval-suite-baseline - 执行真实检索并对比 eval/eval_suite_baseline.json"
	@echo "make eval-suite-update-baseline - 执行真实检索并更新 eval/eval_suite_baseline.json"
	@echo "make run     - 启动多库多对话控制台 (python -m cogdoc.cli)"
	@echo "make debug   - 启动独立 Debug 控制台 (python -m cogdoc.debug)"
	@echo "make serve   - 启动 FastAPI 服务 (uvicorn cogdoc.api.app:app)"
	@echo "make frontend - 加载 .env 后启动 Streamlit 前端 (src/cogdoc/frontend/app.py)"

install:
	$(PYTHON) -m pip install -e ".[dev]"

# 编辑过 rust_core/src 下任何 .rs 后都必须重跑，否则加载的是旧 .so。
native:
	cd rust_core && $(MATURIN) develop --release

check:
	$(PYTHON) scripts/check_native.py

lint:
	$(RUFF) check . \
		--extend-per-file-ignores "scripts/backup_state.py:E402" \
		--extend-per-file-ignores "scripts/check_native.py:E402" \
		--extend-per-file-ignores "scripts/eval_agent.py:E402" \
		--extend-per-file-ignores "scripts/eval_quality.py:E402" \
		--extend-per-file-ignores "scripts/eval_suite.py:E402"

typecheck-security:
	$(MYPY) $(SECURITY_TYPECHECK_MODULES)

test:
	$(PYTHON) -m pytest

smoke-api:
	$(PYTHON) scripts/smoke_api.py

smoke-account-auth:
	$(PYTHON) scripts/smoke_account_auth.py

RELIABILITY_EVAL_DATA_DIR ?= $(abspath $(RELIABILITY_DIR)/eval-data)
RELIABILITY_EVAL_KB_ID ?= arch_blueprint_2026
RELIABILITY_EVAL_SOURCE_DIR ?= $(abspath your_documents)

# 发布可靠性门禁。普通开发目标保持原行为，只有该目标施加 hard timeout。
reliability-gate: lint typecheck-security
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_NATIVE_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/native-timeout.json --cwd rust_core -- $(MATURIN) develop --release --locked
	$(PYTHON) scripts/run_guarded.py --timeout 60 --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/check-timeout.json -- $(PYTHON) scripts/check_native.py
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_SMOKE_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/smoke-timeout.json -- $(PYTHON) scripts/smoke_api.py
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_SMOKE_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/account-smoke-timeout.json -- $(PYTHON) scripts/smoke_account_auth.py
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_TEST_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/test-timeout.json -- $(PYTHON) -m pytest
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_EVAL_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/eval-index-timeout.json -- env COGDOC_DATA_DIR=$(RELIABILITY_EVAL_DATA_DIR) $(PYTHON) scripts/prepare_eval_index.py --kb-id $(RELIABILITY_EVAL_KB_ID) --source-dir $(RELIABILITY_EVAL_SOURCE_DIR) --eval-set eval/retrieval_eval.jsonl --json $(RELIABILITY_DIR)/eval-index.json
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_EVAL_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/eval-timeout.json -- env COGDOC_DATA_DIR=$(RELIABILITY_EVAL_DATA_DIR) $(PYTHON) scripts/eval_suite.py $(EVAL_SUITE_RELEASE_ARGS) --json $(RELIABILITY_DIR)/eval-suite.json
	$(PYTHON) scripts/run_guarded.py --timeout $(RELIABILITY_SOAK_TIMEOUT) --grace $(RELIABILITY_GRACE) --diagnostic $(RELIABILITY_DIR)/soak-timeout.json -- $(SHELL) -c 'set -euo pipefail; $(PYTHON) -m uvicorn cogdoc.api.app:app --host $(RELIABILITY_API_HOST) --port $(RELIABILITY_API_PORT) --timeout-graceful-shutdown $(UVICORN_GRACEFUL_SHUTDOWN_SECONDS) & pid=$$!; cleanup() { kill -TERM $$pid 2>/dev/null || true; wait $$pid 2>/dev/null || true; }; trap cleanup EXIT INT TERM; $(PYTHON) scripts/soak_api.py --url $(RELIABILITY_API_URL) --requests $(RELIABILITY_SOAK_REQUESTS) --concurrency $(RELIABILITY_SOAK_CONCURRENCY) --timeout $(RELIABILITY_REQUEST_TIMEOUT) --startup-timeout 30 --min-success-rate $(RELIABILITY_MIN_SUCCESS_RATE) --max-p95-ms $(RELIABILITY_MAX_P95_MS) --json $(RELIABILITY_DIR)/soak.json'

backup:
	$(PYTHON) scripts/backup_state.py

eval:
	$(PYTHON) scripts/eval_retrieval.py

eval-coverage:
	$(PYTHON) scripts/eval_retrieval.py --coverage-only

eval-retrieval-report:
	$(PYTHON) scripts/eval_retrieval.py --coverage-profile baseline --check-coverage --rerank --gate eval/retrieval_gate.json --json eval/retrieval_eval_report.json

eval-retrieval-baseline:
	$(PYTHON) scripts/eval_retrieval.py --coverage-profile baseline --check-coverage --rerank --json eval/retrieval_eval_baseline.json

eval-retrieval-gate:
	$(PYTHON) scripts/eval_retrieval.py --coverage-profile baseline --check-coverage --rerank --gate eval/retrieval_gate.json --baseline eval/retrieval_eval_baseline.json --json eval/retrieval_eval_report.json

MULTI_ROUTE_EVAL_REPORT ?= artifacts/reliability/multi-route-eval.json
MULTI_ROUTE_CALIBRATION_REPORT ?= artifacts/reliability/multi-route-calibration.json

eval-multi-route:
	$(PYTHON) scripts/eval_multi_route_retrieval.py --eval-set eval/retrieval_eval.jsonl --output $(MULTI_ROUTE_EVAL_REPORT)

calibrate-multi-route:
	$(PYTHON) scripts/calibrate_multi_route_retrieval.py $(MULTI_ROUTE_EVAL_REPORT) --output $(MULTI_ROUTE_CALIBRATION_REPORT)

eval-quality:
	$(PYTHON) scripts/eval_quality.py

eval-quality-coverage:
	$(PYTHON) scripts/eval_quality.py --check-coverage

eval-suite:
	$(PYTHON) scripts/eval_suite.py

eval-suite-run-retrieval:
	$(PYTHON) scripts/eval_suite.py --run-retrieval

eval-suite-report:
	$(PYTHON) scripts/eval_suite.py $(EVAL_SUITE_RELEASE_ARGS) --json eval/eval_suite_report.json

eval-suite-baseline:
	$(PYTHON) scripts/eval_suite.py $(EVAL_SUITE_RELEASE_ARGS) --baseline eval/eval_suite_baseline.json

eval-suite-update-baseline:
	$(PYTHON) scripts/eval_suite.py $(EVAL_SUITE_RELEASE_ARGS) --update-baseline eval/eval_suite_baseline.json

run:
	$(PYTHON) -m cogdoc.cli

debug:
	$(PYTHON) -m cogdoc.debug

serve:
	$(PYTHON) -m uvicorn cogdoc.api.app:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown $(UVICORN_GRACEFUL_SHUTDOWN_SECONDS)

frontend:
	@if [[ -f .env ]]; then \
		$(PYTHON) -m dotenv run --no-override -- \
			$(PYTHON) -m streamlit run src/cogdoc/frontend/app.py; \
	else \
		$(PYTHON) -m streamlit run src/cogdoc/frontend/app.py; \
	fi
