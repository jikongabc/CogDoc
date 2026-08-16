# CogDoc

> ⭐ **If CogDoc helps you, please drop a star** — it keeps the project moving and new features coming.

[English](README.md) · [简体中文](docs/README_zh-CN.md)

A local RAG knowledge-base console for individuals and teams, built on **LangGraph multi-agent orchestration** with a **deterministic Rust core (PyO3 + maturin)** underneath. It answers questions, summarizes a single document, compares multiple documents, and turns feedback into reviewable derived knowledge over your own PDF knowledge base — and every generated claim is pinned back to a `[source:Pn]` citation that is *checked, not trusted*. Use it from a **CLI console**, a **Streamlit web app** backed by FastAPI, or a standalone **Debug console** for trace inspection.

> **Optional local OCR.** OCR is disabled by default. When enabled, pages without enough usable native text are rendered with PyMuPDF and recognized by a local Tesseract CLI; native-text pages keep the existing fast path.

## Feature Highlights

- **Grounded QA with verified citations** — generation is constrained to retrieved document blocks; fabricated file/page tags are caught by a Rust validator and re-generated in a self-heal loop.

- **Optional claim-level semantic gate** — after physical citation validation, an independent verifier can audit each factual claim against only its cited evidence, attempt a bounded repair, and fail closed to a stable refusal if support still cannot be established.

- **Requirement-aware evidence gate** — multi-part questions are decomposed into at most three atomic evidence requirements, retrieved with explicit provenance, checked one by one against a closed set of chunks, and given one bounded recovery round by default before a fail-closed refusal.

- **Structured single-document summary** — fixed sections, deterministic citations bound from chunk metadata.

- **Multi-document comparison** — per-document profiles across fixed dimensions, rendered as cited dimension-by-dimension blocks.

- **Hybrid retrieval, query-level fusion** — each original, rewritten, and requirement-specific query searches the PDF vector+BM25 hybrid channel and the approved-derived-knowledge channel; the resulting query/channel rankings are fused with equally weighted deterministic RRF, and a requirement quota prevents late focused queries from being starved before rerank. Tokenization and BM25 are native — Chinese via `jieba-rs`, English lowercased + Snowball-stemmed + stopword-filtered.

- **Structure-aware Parent–Child context** — conservative Markdown, numbered, and common Chinese/English headings form section parents. Retrieval and citations remain child-chunk precise, while reranked hits can hydrate a bounded contiguous sibling window from the same section; legacy or unstructured indexes fall back to the existing ±1 neighbor window.

- **Content-addressed incremental cache** — a per-file SHA-256 manifest plus a versioned chunk-identity contract: unchanged files reuse the existing index, and only a changed PDF or chunking scheme triggers an incremental rebuild.

- **Multiple knowledge bases · multiple conversations · layered memory** — full display history is persisted for replay; validated recent turns form bounded short-term memory, evicted turns become session-level summaries and decisions, and only explicit stable facts enter cross-session long-term memory. Wrong answers never enter Agent memory.

- **Deep Research workspace with reproducible evidence** — generate or edit an outline whose sections contain atomic evidence requirements, execute every requirement through the production hybrid retrieval path, and pause/resume/cancel through durable attempt leases. Admission, phase deadlines, retrieval/document/LLM/input budgets, and stale-worker commits are bounded fail-closed. Report claims are independently audited, every atomic requirement must be covered by supported cited claims, and the two gates share at most one repair before failing closed. Each run freezes index/source/derived-knowledge/tuning and retrieval-contract provenance; stale evidence must be refreshed before review or publication, and publication produces an integrity-checked Markdown snapshot plus a deterministic verification bundle. A paginated, ETag-aware summary index keeps collection polling independent of report and evidence size.

- **Web, CLI, and Debug entry points** — a slash-command CLI console, a Streamlit web UI over FastAPI, and a focused `make debug` console for trace inspection.

- **Derived knowledge review loop** — manually add knowledge, save validated answers, or turn corrections / no-evidence feedback into pending knowledge cards with source bindings, conflict groups, stale scans, revisions, batch approve/reject, and archive/delete flows.

- **Feedback analysis and attributed retrieval tuning** — thumbs-up/down, corrections, ratings, issue types, and evidence context are persisted by `trace_id`; positive signals may boost cited chunks, while negative signals penalize them only when `feedback_type=bad_retrieval`. `skip_retrieval_feedback=true` disables tuning for an entry, and every tuning record remains reviewable and rollbackable.
- **Evidence-level retrieval-eval flywheel** — a complete successful server trace plus an explicit `thumbs_down` / `bad_retrieval` signal creates an unlabelled review draft for QA requirements, Summary sections, or Compare source×dimension cells. Gold chunks/spans and hard negatives can only be added by an authorized reviewer; approvals and exports fail closed when the index generation, chunk identity contract, or source SHA has changed.

- **Trace observability, review queue, and webhooks** — every request can export a safe JSON trace with config, node timings, rewrites, evidence previews, and errors; the web UI scopes traces to the current conversation, aggregates pending/stale knowledge and feedback into a review queue, and can emit webhook events for new pending knowledge.

- **Real accounts, team workspaces, and retrieval ACLs** — optional persistent email/password accounts provide revocable sessions, workspace membership, invitations, RBAC, and tenant-isolated state. Knowledge-base/document policies and subject grants are enforced inside vector and BM25 recall before top-k, then checked again before evidence reaches prompts, traces, or background Research.

## Feature Walkthrough

1. **Web chat with citations and evidence.** Pick a knowledge base, ask in natural language, watch live execution progress, then receive the finalized answer and inspect its citation sources, evidence snippets, and feedback controls.

   <img src="./docs/images/web-chat.png" alt="Web chat" width="800">

2. **CLI console.** A slash-command console for knowledge bases, ingestion, multi-conversation history, and forced task modes.

   <img src="./docs/images/cli-console1.png" alt="CLI console" width="800">

3. **Standalone Debug console.** `make debug` opens a focused console for one KB; after a normal answer, continue with `/trace`, `/steps`, `/rewrite`, `/evidence`, and `/config`, or run `/retrieve <query>` to inspect retrieval without calling the LLM.

   <img src="./docs/images/debug-console1.png" alt="Standalone Debug console" width="800">

4. **Grounded QA.** Every factual sentence ends with a citation whose file name and page exist in the retrieved context; invalid ones bounce the answer back for regeneration.

   <img src="./docs/images/qa_net.png" alt="Grounded QA web view" width="800">

5. **Structured summary.** Summarize one named document into fixed sections with deterministic per-section citations.

   <img src="./docs/images/summary_net.png" alt="Structured summary web view" width="800">

6. **Multi-document comparison.** Compare two or more named documents method-by-method, metric-by-metric, with citations on every cell.

   <img src="./docs/images/compare_net.png" alt="Comparison web view" width="800">

7. **Trace debug panel.** Inspect only the current conversation's traces, including routing, query rewrites, retrieval/rerank steps, request config, and citation audits.

   <img src="./docs/images/web-trace-debug.png" alt="Trace debug panel" width="800">

8. **Derived knowledge review center.** Add manual knowledge, save an answer, inspect source bindings, review conflicts, approve/reject/archive items, and rebuild the approved derived-knowledge index.

   <img src="./docs/images/derived-knowledge3.png" alt="Derived knowledge review center" width="800">

9. **Feedback and tuning.** Every thumbs-up/down, correction, and no-evidence feedback entry is tied to the answer's `trace_id`, query, answer, citations, and evidence; CogDoc turns bad cases into the evaluation ledger, converts fixable content into pending derived knowledge, and creates enable/disable retrieval-tuning records so future ranking can keep improving from human feedback.

   <img src="./docs/images/feedback.png" alt="Feedback and tuning" width="800">

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,frontend]"   # runtime + build/test + Streamlit deps
make native     # build the Rust extension: cd rust_core && maturin develop --release
make check      # verify the extension and its native symbols
make run        # build/reuse the index, warm up models, start the console
```

Dependencies live in [pyproject.toml](pyproject.toml): runtime in `[project.dependencies]`, with `dev` (build/test) and `frontend` (Streamlit client) as optional extras — install both for the full local experience via `.[dev,frontend]`. The package uses a `src/` layout (`src/cogdoc/`); the `make` targets put `src/` on `PYTHONPATH`, so no install is strictly required to run the suite.

Copy `.env.example` to `.env` and set at least your cloud `LLM_API_KEY` (or run `/local` with Ollama). Put PDFs in the inbox `your_documents/` (or set `COGDOC_DOC_DIR`). `make native` must be re-run after any change under `rust_core/src/` — the `.so` is not auto-rebuilt and not committed.

Persistent accounts default to off for a no-surprise upgrade. For an individual or team deployment, set `COGDOC_ACCOUNT_AUTH_ENABLED=true`, start the API, register the first owner, and use its returned Bearer token. An enterprise can then invite the remaining members and set `COGDOC_SELF_REGISTRATION_ENABLED=false`.

## How to Use

The CLI and web app share the same KB → ingest → ask flow. Set up once (see [Quick Start](#quick-start)): install deps, build the native extension (`make native && make check`), configure `.env`, and drop PDFs into `your_documents/`.

### CLI console

```bash
make run            # python -m cogdoc.cli
```

Then drive everything with slash commands inside the console:

1. `/kb new <name>` — create a knowledge base, `/kb` to list / switch.
2. `/add <file.pdf>` — ingest an inbox PDF from `your_documents/` into the active KB (synchronous rebuild).
3. `/new` — start a conversation; `/chats` and `/open` browse persisted history.
4. Ask directly to run **QA**; "summarize `<file>`" runs **Summary**; "compare `<a>` and `<b>`" runs **Compare**.
5. `/cloud` uses the cloud LLM, `/local` uses Ollama; `/help` lists commands; `exit` quits.
6. Use `/dk` or `/knowledge` for derived knowledge, `/feedback` for feedback records and analyses, `/tuning` for retrieval-weight controls, and `/review` for queue summaries, metrics, and export.

`make debug` opens the standalone Debug console for one KB. Ask questions there to get normal answers plus trace summaries, use `/trace`, `/steps`, `/rewrite`, `/evidence`, and `/config` to inspect the latest request, or run `/retrieve <query>` to inspect retrieval and rerank output without calling the LLM. To debug a specific KB directly, run `python -m cogdoc.debug --kb <kb_id>`.

### Web app (Streamlit + FastAPI)

```bash
make serve          # terminal 1: FastAPI at http://localhost:8000
make frontend       # terminal 2: Streamlit UI (opens in the browser)
```

In the browser:

1. **Account** — when account auth is enabled, register or sign in, then choose one of your personal/team workspaces.
2. **Sidebar → Knowledge base** — create a KB, or select an existing one.
3. **Sidebar → Documents** — upload a PDF and ingest it; a progress panel polls the background index job until it finishes.
4. **Conversations** — start a new conversation or reopen a previous one (session and KB persist in the URL, so a refresh resumes the same chat).
5. **Chat** — pick a mode (`auto` / `qa` / `summary` / `compare`), ask, watch live progress, and read the finalized answer with its citation sources, evidence snippets, and 👍/👎 feedback.
6. Toggle **Local Ollama mode** in the sidebar to route generation to the local model.
7. Open **Debug** to inspect traces for the current conversation only, or use **Retrieval debug** to call `/v1/retrieve` directly and inspect chunk hits, rerank scores, and retrieval metadata.
8. Switch the main view to **Derived Knowledge** to create knowledge, review pending/stale items, inspect feedback analysis, enable/disable retrieval tuning, export the review queue, and scan for stale bindings after document changes.

### Calling the API directly

The Streamlit app is a thin client over the FastAPI service — you can hit it directly:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/auth/config`, `POST /v1/auth/register`, `POST /v1/auth/login` | Discover account mode, create an account/personal workspace, or issue an opaque Bearer session |
| `GET /v1/auth/me`, `POST /v1/auth/logout`, `POST /v1/auth/logout-all` | Inspect the current identity or revoke one/all login sessions |
| `GET /v1/auth/sessions`, `DELETE /v1/auth/sessions/{id}`, `POST /v1/auth/change-password` | Manage devices/sessions and rotate the password |
| `GET/POST /v1/workspaces`, `POST /v1/workspaces/{id}/switch` | List/create workspaces and switch the current session's active workspace |
| `GET /v1/workspaces/{id}/members`, `PATCH/DELETE /v1/workspaces/{id}/members/{member_id}` | List members, change roles, or remove a member |
| `POST/GET /v1/workspaces/{id}/invites`, `DELETE /v1/workspaces/{id}/invites/{invite_id}`, `POST /v1/auth/invitations/accept` | Issue, inspect, revoke, or accept one-time workspace invitations |
| `GET /v1/tenant` | Inspect the authenticated workspace, subject, role, permissions, and quota usage |
| `GET /v1/audit-events` | Page through the current workspace's hash-chained audit metadata (owner/admin) |
| `POST /v1/knowledge-bases`, `GET /v1/knowledge-bases` | Create / list knowledge bases |
| `GET/PATCH /v1/knowledge-bases/{kb}/access` | Inspect or change a KB's `workspace` / `private` policy |
| `GET/PATCH /v1/knowledge-bases/{kb}/documents/{document_id}/access` | Inspect or change a document's `inherit` / `workspace` / `private` policy |
| `GET/POST/DELETE .../access/grants[/subject_id]` | Manage subject grants at either KB or document scope |
| `POST /v1/knowledge-bases/{kb}/documents` | Upload + ingest a PDF (returns an async `job_id`) |
| `GET /v1/knowledge-bases/{kb}/sources`, `GET /v1/knowledge-bases/{kb}/sources/{source}/chunks` | Browse indexed sources and chunk previews |
| `GET /v1/index-jobs/{job_id}` | Poll ingestion progress |
| `POST /v1/chat`, `POST /v1/chat/stream` | Ask (JSON or SSE streaming) |
| `POST /v1/summary`, `POST /v1/compare` | Run explicit Summary / Compare tasks without router ambiguity |
| `POST /v1/retrieve` | Return child-level retrieval hits with parent/section identity, source/page previews, and ranking metadata |
| `GET /v1/sessions`, `GET /v1/sessions/{id}/history` | List / replay conversation history |
| `GET /v1/sessions/{id}/memory` | Inspect short-, mid-, and long-term memory |
| `DELETE /v1/memory/long-term?doc_id=...` | Forget long-term memory for one knowledge base |
| `GET /v1/traces?doc_id=...&session_id=...` | List recent traces, optionally scoped to one KB/session |
| `GET /v1/traces/{trace_id}` | Fetch an exported request trace |
| `POST /v1/feedback` | Submit thumbs-up/down on a `trace_id` |
| `GET /v1/feedback`, `GET /v1/feedback-analysis` | Browse feedback records and structured feedback understanding results |
| `POST /v1/knowledge`, `GET /v1/knowledge` | Create / list derived knowledge entries |
| `POST /v1/knowledge/{id}/approve`, `/reject`, `/archive`, `/revise` | Review or revise derived knowledge |
| `POST /v1/knowledge/batch-approve`, `POST /v1/knowledge/batch-reject` | Batch review derived knowledge |
| `GET /v1/knowledge/pending-count`, `GET /v1/knowledge/index-status`, `POST /v1/knowledge/stale-scan` | Inspect pending/stale counts, derived-knowledge index state, and stale source bindings |
| `GET /v1/review-queue`, `GET /v1/review-queue/export` | Summarize and export the review queue |
| `GET /v1/feedback-loop-metrics` | Return feedback / review / tuning loop metrics |
| `GET /v1/retrieval-feedback`, `POST /v1/retrieval-feedback/{id}/enable`, `POST /v1/retrieval-feedback/{id}/disable` | Inspect or roll back feedback-derived retrieval tuning |
| `GET /v1/retrieval-eval-drafts`, `GET /v1/retrieval-eval-drafts/{id}` | Review pending evidence-unit annotations and their live stale status |
| `POST /v1/retrieval-eval-drafts/{id}/review` | Approve/reject a draft with optimistic revision checking |
| `GET /v1/retrieval-eval-drafts/export` | Export approved, non-stale training or release-gate rows; QA-only export reports excluded Summary/Compare drafts explicitly |
| `POST /v1/research-jobs`, `GET /v1/research-jobs` | Create or list persistent research plans |
| `GET /v1/research-jobs/{id}`, `PUT /v1/research-jobs/{id}/plan` | Inspect or revision-safely edit a research outline |
| `POST /v1/research-jobs/{id}/plan/auto` | Generate an editable outline with 1–3 atomic evidence requirements per section |
| `POST /v1/research-jobs/{id}/start`, `/pause`, `/resume`, `/cancel` | Control durable section-by-section evidence retrieval |
| `GET /v1/research-jobs/{id}/provenance`, `POST /v1/research-jobs/{id}/refresh` | Inspect the frozen evidence inputs or archive and refresh a stale run |
| `POST /v1/research-jobs/{id}/generate`, `GET /v1/research-jobs/{id}/report` | Closed-set verify section evidence, generate cited sections, and download the Markdown report |
| `PUT /v1/research-jobs/{id}/review`, `POST /v1/research-jobs/{id}/publish` | Review each generated section or evidence gap with optimistic revision protection, then publish only a fully reviewed report |
| `GET /v1/research-jobs/{id}/published-report`, `/published-bundle` | Download the integrity-checked Markdown snapshot or deterministic ZIP verification bundle |
| `GET /healthz`, `GET /readyz`, `GET /metrics` | Health, readiness, Prometheus metrics |

`/v1/chat/stream` always streams lifecycle and node-progress events. For QA,
Summary, and Compare, model text is intentionally buffered because intermediate
output can contain internal Evidence IDs and has not yet passed finalization; the
client receives the finalized answer as one `token` event immediately before the
normal `final` event, not as token-by-token prose. Other tasks retain live model
tokens unless the global claim-verification gate requires buffering.

### Accounts, workspaces, and RAG authorization

`COGDOC_ACCOUNT_AUTH_ENABLED=false` is the backward-compatible default. Set it to `true` for persistent human identities: registration atomically creates a user, an owner membership, a personal workspace, and a login session. Passwords must contain at least 12 characters and are stored as versioned, salted scrypt hashes. Session and invitation values are opaque bearer secrets returned only at issuance; only SHA-256 digests are persisted, and invitations are single-use. Sessions expire, can be listed/revoked individually or globally, and password changes revoke the other active sessions. Repeated bad logins trigger a configurable temporary lock. Send the returned token as `Authorization: Bearer <token>`; it is not a cookie and must never be put in URLs or logs.

Owners can manage the workspace itself; admins combine write, delete, review/publish, and access-management permissions; editors read/query/write; reviewers read/query/review/publish; viewers read/query. Owners/admins manage members and invitations, but a normal role edit cannot manufacture another owner or remove the last owner. Switching workspaces changes the legacy active workspace of that login session. Current clients additionally send the non-secret `X-CogDoc-Workspace` selector on every protected request, so two browser tabs sharing one Bearer can stay pinned to different memberships; the server still validates that membership, and a selector conflicting with a `/v1/workspaces/{id}` path fails closed as an opaque 404. Omitting the header preserves compatibility with clients that rely on the session's active workspace. Knowledge bases with the same public slug are physically isolated by workspace, while account users' conversation sessions and traces are additionally namespaced by user. Cross-workspace list, detail, and mutation requests appear absent rather than revealing another tenant's resource.

New knowledge bases default to `workspace`; uploaded documents default to `inherit`. A `private` KB is visible only to its resource owner, workspace owner/admin, or a same-workspace subject with a KB grant. Documents may inherit the KB, reopen visibility to the workspace, or become private; a document grant can expose only that document. Grant roles and workspace roles are intersected, so an ACL grant cannot elevate a user's workspace permissions. Missing, malformed, or unavailable ACL state denies access in account mode. Only owner/admin identities with `manage_access` can alter policies or grants.

Query authorization is materialized as an explicit `ALL`, non-empty `SUBSET`, or `DENY` scope. For a subset, Chroma vector filtering, BM25 candidate selection, approved-derived-knowledge lookup, Summary, Compare, and QA all apply the source allowlist before top-k/reranking; a second post-fusion guard removes any result returned by a stale or custom backend that ignored the filter. This avoids unauthorized high-scoring chunks crowding authorized evidence out of top-k and prevents forbidden text from entering prompts, traces, or persisted evidence. Background Research freezes its creator and exact source boundary, rechecks current workspace membership and ACLs before/after retrieval, refuses a backend that cannot enforce a subset, and stops if access has been revoked; later grants do not silently broaden an already-running job.

Static service principals remain available for automation and staged upgrades. `COGDOC_API_PRINCIPALS` maps each key to a `tenant_id`, `subject_id`, and role; legacy `COGDOC_API_KEYS` and `COGDOC_EVAL_REVIEW_API_KEYS` remain `default`-workspace admins. When account auth is enabled, configured service keys and human sessions coexist. With account auth disabled, authentication is open only when all three static credential settings are empty. Bearer takes precedence over `X-API-Key` if both are sent. Explicit reviewer/admin/owner principals can use evidence-eval and Research review routes; persisted actors always come from the authenticated identity. Hard quotas cover KB count, committed plus in-flight PDF count, and PDF bytes. `/v1/tenant` reports `limits`, `usage`, and `reserved`; quota failures return HTTP 409 with `TENANT_QUOTA_EXCEEDED`.

`COGDOC_API_KEY` (singular) is an outbound credential consumed by the Streamlit/CLI client, not a server credential allowlist. If it is present in the Streamlit process and the browser has no human session token, the UI deliberately enters service-key mode and skips the account login screen. Leave it empty on every shared or multi-user frontend; configure accepted service identities on the API with `COGDOC_API_KEYS` / `COGDOC_API_PRINCIPALS`, and let human users sign in normally. A singular key is appropriate only for a trusted single-user console or dedicated automation frontend.

The module-level production app durably appends metadata-only events to `COGDOC_DATA_DIR/audit/events.jsonl`: mutations receive an intent record before execution and an HTTP response-commit record before response headers are sent; reads receive the latter. A corrupt or unwritable chain makes protected traffic fail closed with HTTP 503. `GET /v1/audit-events` returns the current workspace newest-first, with an exclusive `before_sequence` cursor. Public probes/docs and unauthenticated 401 attempts are not recorded. The per-workspace SHA-256 chain detects malformed, truncated, rewritten, or broken history during a live process and validates self-consistency after restart, but it is not a signature, WORM store, or external trusted head; back up and externally anchor it when adversarial filesystem tampering is in scope.

### Layered memory

| Layer | Scope | Content | Storage and forgetting |
| --- | --- | --- | --- |
| Working / short-term | One graph run and the current session | Current goal, task status, tool state, and the latest citation-validated turns | Graph state plus a bounded SQLite session window; dual message/character budgets evict the oldest turns |
| Mid-term | One session | Extractive summaries of evicted turns, explicit goals, and decisions | `sessions.mid_memory`; removed with the session |
| Long-term | One knowledge base across sessions | Only explicit memories, stable preferences, policies, and project facts | Deduplicated `long_memories` rows with an importance/capacity limit; removable through the API |

Full UI history remains separate from Agent memory. The default budgets are 12 short-term messages, 6,000 short-term characters, a 4,000-character mid-term summary, 64 stored long-term facts, and 8 long-term facts injected per request. Configure them with the `COGDOC_MEMORY_*` variables in `.env.example`.

Memory recall is query-aware. CogDoc runs short-term recency, mixed-language lexical recall, long-term importance/recency, and optional BGE-M3 semantic recall as independent channels; weighted RRF merges their ranks before per-layer context packing. A configurable recent-message prefix is pinned for continuity. Short-term semantic recall is disabled by default because recency and lexical channels already cover the small working set, but it can be enabled independently. If embedding fails, recall degrades to the remaining channels without failing the chat request. All channel weights and limits are configurable through `COGDOC_MEMORY_*` variables.

## Optional scanned-page OCR

OCR is an opt-in ingestion fallback, not a replacement for native PDF extraction. CogDoc first reads every page's text layer. After whitespace normalization, a page with fewer than `COGDOC_OCR_MIN_NATIVE_CHARS` characters is an OCR candidate; pages above that threshold are never rendered for OCR. Candidate pages are rendered by the existing PyMuPDF dependency and passed to the local Tesseract CLI.

Docker images already contain Tesseract plus the `eng` and `chi_sim` language packs. For a local Debian/Ubuntu installation:

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim
tesseract --list-langs
```

On other platforms, install the Tesseract executable and the language data required by `COGDOC_OCR_LANGUAGES`, then set `COGDOC_OCR_BINARY` to its executable path if it is not on `PATH`. No additional Python OCR package is required.

```dotenv
COGDOC_OCR_ENABLED=true
COGDOC_OCR_PROVIDER=tesseract
COGDOC_OCR_BINARY=tesseract
COGDOC_OCR_LANGUAGES=eng+chi_sim
COGDOC_OCR_DPI=300
COGDOC_OCR_MIN_NATIVE_CHARS=40
COGDOC_OCR_MAX_PAGES=100
COGDOC_OCR_PAGE_TIMEOUT_SECONDS=30
COGDOC_OCR_REQUIRED=false
```

`COGDOC_OCR_MAX_PAGES` bounds OCR candidate pages per document, and `COGDOC_OCR_PAGE_TIMEOUT_SECONDS` bounds each page-level Tesseract invocation. Raising DPI can improve small-print recognition but increases CPU and memory use. With `COGDOC_OCR_REQUIRED=false`, a missing binary, unavailable language pack, timeout, or non-zero OCR exit degrades that page to its native-text result and ingestion continues. With `COGDOC_OCR_REQUIRED=true`, the same condition fails ingestion so an incomplete scanned document cannot be accepted silently.

Rendering and recognition run in the CogDoc process and a local Tesseract subprocess; no page image is sent to a hosted OCR service. OCR can still expose recognized text to the existing embedding and LLM pipeline, so cloud model backends retain their existing data boundary. Keep OCR disabled for untrusted PDFs unless the deployment can afford the extra CPU, memory, and subprocess work.

`GET /health/ready` exposes OCR as a separate component. Its default state is `disabled`. When OCR is enabled but its binary is missing, optional OCR (`COGDOC_OCR_REQUIRED=false`) reports `degraded` while the service remains ready; required OCR (`COGDOC_OCR_REQUIRED=true`) makes the readiness probe return HTTP 503. Page-level recognition failures follow the ingestion behavior described above rather than changing readiness after the binary check succeeds.

## Unified state backend

`COGDOC_STATE_BACKEND` selects persistence for application state and defaults to `jsonl` for a backward-compatible rollout. Keep it set to `jsonl` until the migration has completed and verification succeeds:

```bash
python scripts/migrate_state.py                 # dry-run; writes nothing
python scripts/migrate_state.py --apply         # import legacy state
python scripts/migrate_state.py --verify-only   # verify the imported state
```

Only after all three steps succeed should `.env` be changed to `COGDOC_STATE_BACKEND=sqlite`. The unified SQLite backend stores sessions, index jobs, research plans, feedback records, feedback analyses, derived knowledge, retrieval-feedback/tuning state, and retrieval-eval drafts together in `COGDOC_DATA_DIR/state.db`. The migration copies the six research/feedback/knowledge/evaluation state families from their legacy stores.

`COGDOC_FEEDBACK_STORE` remains only for compatibility with deployments that still select the legacy standalone feedback backend. It does not select the unified backend and should not be used instead of `COGDOC_STATE_BACKEND` after migration.

## Tech Stack

- **Deterministic core** — a custom [Rust](https://www.rust-lang.org/) extension ([PyO3](https://pyo3.rs/) + [maturin](https://www.maturin.rs/)) carries `jieba-rs` CN/EN tokenization, BM25, RRF fusion, SHA-256 manifest, and citation validation — all native, independently unit-tested, stable across agent/prompt churn.
- **Retrieval** — `bge-m3` multilingual vector recall + BM25 keyword recall, fused by the Rust RRF kernel and reranked by `bge-reranker-v2-m3`; PDF vectors and approved derived-knowledge vectors live in [Chroma](https://www.trychroma.com/), PDFs are parsed by PyMuPDF.
- **Orchestration** — [LangGraph](https://langchain-ai.github.io/langgraph/) wires routing → task subgraphs → physical citation self-heal → optional parent-graph claim audit / bounded repair / refusal into a loopable state graph.
- **Models** — OpenAI-compatible dual backend, hot-swappable: cloud DeepSeek or local Ollama `qwen2.5:7b`.
- **Serving and observability** — FastAPI with SSE streaming, optional persistent-account/service-key auth, workspace RBAC/resource ACLs, and token-bucket rate limiting; sessions, index jobs, feedback, review queues, and derived knowledge are persisted locally; JSON traces feed the web Trace panel and standalone Debug console.

## Architecture

>  **Solid lines** → runtime call / data flow &nbsp;|&nbsp; **Dashed lines** → startup / safeguard relations

**Runtime Path**

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"linear","nodeSpacing":35,"rankSpacing":45}}}%%
flowchart TD
    classDef node fill:#ffffff,stroke:#8c959f,stroke-width:1px,color:#24292f
    classDef core fill:#eef6ff,stroke:#54aeef,stroke-width:1px,color:#24292f
    classDef guard fill:#fff1f1,stroke:#ff8182,stroke-width:1px,color:#24292f
    classDef native fill:#fff8c5,stroke:#d4a72c,stroke-width:1px,color:#24292f

    subgraph ENTRY["Entry points"]
        CLI["CLI console"]
        DEBUG["Debug console"]
        WEB["Streamlit web UI"]
    end

    subgraph HTTP["FastAPI HTTP API"]
        APISTART["app startup"]
        ACCESS["API key auth / rate limit / metrics"]
        ROUTES["routes: chat / agent / documents / knowledge / feedback / traces / health"]
    end

    subgraph CORE["Core Python services"]
        SERVICE["service functions"]
        CHAT["chat service"]
        INGEST["ingest service"]
        REVIEW["review queue / webhooks"]
    end

    subgraph SAFETY["Runtime safeguards"]
        PROCLOCK["startup gate / single-instance lock"]
        JOURNAL["mutation journal / startup recovery"]
        KBLOCK["per-KB write lock"]
    end

    subgraph GRAPH["LangGraph workflow"]
        ROUTER["RouterAgent"]
        QA["QA subgraph: rewrite / verify / retrieve / rerank / generate / self-heal"]
        SUMMARY["Summary subgraph: loader / plan / sections / global"]
        COMPARE["Compare subgraph: loader / profile / table / citation"]
        CLAIMAUDIT["Claim verifier: semantic evidence audit"]
        CLAIMREPAIR["Claim repairer: bounded repair / citation recheck"]
        CLAIMBLOCK["Fail-closed refusal"]
        CLAIMFINAL["Audited final answer / stable refusal"]
    end

    subgraph BACKENDS["Model and native backends"]
        LLM["LLM clients: Cloud / Ollama"]
        EMB["Embedding / rerank: bge-m3 / bge-reranker-v2-m3"]
        RUST["Rust core: tokenize / BM25 / RRF / SHA-256 / citation check"]
    end

    CLI --> SERVICE
    DEBUG --> SERVICE
    WEB --> ACCESS
    APISTART -.-> ACCESS
    APISTART -.-> ROUTES
    ACCESS --> ROUTES
    ROUTES --> SERVICE

    SERVICE --> CHAT
    SERVICE --> INGEST
    SERVICE --> REVIEW

    CHAT --> ROUTER
    ROUTER --> QA
    ROUTER --> SUMMARY
    ROUTER --> COMPARE

    QA --> CLAIMAUDIT
    SUMMARY --> CLAIMAUDIT
    COMPARE --> CLAIMAUDIT
    CLAIMAUDIT -->|unsupported| CLAIMREPAIR
    CLAIMREPAIR -->|citation valid| CLAIMAUDIT
    CLAIMREPAIR -->|citation invalid; budget remains| CLAIMREPAIR
    CLAIMAUDIT -->|pass or disabled| CLAIMFINAL
    CLAIMAUDIT -->|error or attempts exhausted| CLAIMBLOCK
    CLAIMREPAIR -->|error or attempts exhausted| CLAIMBLOCK
    CLAIMBLOCK --> CLAIMFINAL

    QA --> LLM
    SUMMARY --> LLM
    COMPARE --> LLM
    CLAIMAUDIT --> LLM
    CLAIMREPAIR --> LLM
    QA --> RUST
    SUMMARY --> RUST
    COMPARE --> RUST
    CLAIMREPAIR --> RUST
    QA --> EMB
    SUMMARY --> EMB
    COMPARE --> EMB
    INGEST --> RUST
    INGEST --> EMB

    CLI -. startup .-> PROCLOCK
    DEBUG -. startup .-> PROCLOCK
    APISTART -. startup .-> PROCLOCK
    PROCLOCK -. recovery .-> JOURNAL
    JOURNAL -. recovered state .-> SERVICE
    INGEST -. write protection .-> KBLOCK

    style ENTRY fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style HTTP fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style CORE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style SAFETY fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style GRAPH fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style BACKENDS fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    class CLI,DEBUG,WEB,APISTART,ROUTES,ACCESS,ROUTER,QA,SUMMARY,COMPARE,CLAIMAUDIT,CLAIMREPAIR,CLAIMFINAL node
    class SERVICE,CHAT,INGEST,REVIEW core
    class PROCLOCK,JOURNAL,KBLOCK,CLAIMBLOCK guard
    class LLM,RUST,EMB native
```

CLI and Debug bypass the FastAPI HTTP adapter; they call the same Python services in-process. The Streamlit UI is the built-in entry point that talks to FastAPI over HTTP/SSE. CLI, Debug, and FastAPI all acquire the single-instance process lock at startup, then recover the mutation journal before serving KB mutations.

The next diagram expands ingestion, retrieval, and local persistence boundaries: source PDFs and approved derived knowledge are indexed separately, then joined into one candidate pool at query time; feedback does not rewrite indexes directly, but is persisted as reviewable records or rollbackable retrieval tuning.

**Index, Retrieval, and Storage**

**Indexing and mutation path**

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"linear","nodeSpacing":35,"rankSpacing":45}}}%%
flowchart LR
    classDef node fill:#ffffff,stroke:#8c959f,stroke-width:1px,color:#24292f
    classDef storage fill:#f0fff4,stroke:#4ac26b,stroke-width:1px,color:#24292f
    classDef guard fill:#fff1f1,stroke:#ff8182,stroke-width:1px,color:#24292f
    classDef native fill:#fff8c5,stroke:#d4a72c,stroke-width:1px,color:#24292f

    subgraph SERVICES["Core Python services"]
        INGEST["ingest service"]
        KBMUT["KB mutations: create / delete / upload / reindex"]
    end

    subgraph SAFETY["Mutation safety"]
        PROCLOCK["single-instance lock: acquired at startup"]
        KBLOCK["kb_write_lock"]
        JOURNAL["mutation journal"]
        EPOCH["KB epoch / tombstone"]
    end

    subgraph INGESTION["Ingestion pipeline"]
        PARSE["PDF parse / chunk / manifest"]
    end

    subgraph NATIVE["Rust core"]
        RUST["tokenize / SHA-256 / BM25 / RRF"]
    end

    subgraph STORE["Local storage"]
        PDFVEC["Chroma PDF vectors"]
        BM25["BM25 artifact"]
        ARTIFACTS["artifacts: manifest / journal"]
    end

    PROCLOCK -. recovery .-> JOURNAL
    INGEST --> KBLOCK
    KBMUT --> KBLOCK
    KBLOCK --> PARSE
    KBLOCK --> EPOCH
    EPOCH -. stale guard .-> KBMUT
    KBLOCK --> JOURNAL
    PARSE --> RUST
    PARSE --> PDFVEC
    PARSE --> ARTIFACTS
    RUST --> BM25
    JOURNAL --> ARTIFACTS

    style SERVICES fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style SAFETY fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style INGESTION fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style NATIVE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style STORE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    class INGEST,KBMUT,PARSE node
    class PDFVEC,BM25,ARTIFACTS storage
    class PROCLOCK,KBLOCK,JOURNAL,EPOCH guard
    class RUST native
```

**QA retrieval path**

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"linear","nodeSpacing":35,"rankSpacing":45}}}%%
flowchart LR
    classDef node fill:#ffffff,stroke:#8c959f,stroke-width:1px,color:#24292f
    classDef storage fill:#f0fff4,stroke:#4ac26b,stroke-width:1px,color:#24292f
    classDef native fill:#fff8c5,stroke:#d4a72c,stroke-width:1px,color:#24292f

    subgraph SERVICES["Core Python services"]
        CHAT["chat service"]
    end

    subgraph STORE["Local storage"]
        PDFVEC["Chroma PDF vectors"]
        BM25["BM25 artifact"]
        DKVEC["Chroma derived-knowledge vectors"]
        TUNESTORE["retrieval tuning store: tuning records"]
    end

    subgraph RETRIEVAL["QA retrieval pipeline"]
        QUERY["query + rewrites"]
        VECH["PDF vector recall: Chroma"]
        BM25CH["PDF keyword recall: BM25"]
        DKCH["derived-knowledge channel: vector search"]
        FUSION["PDF RRF fusion"]
        CAND["candidate pool"]
        TUNE["feedback weights"]
        RERANK["bge-reranker-v2-m3"]
        EVIDENCE["evidence for answer"]
    end

    subgraph KNOWLEDGE["Feedback and review loop"]
        APPROVED["approved derived knowledge"]
    end

    subgraph NATIVE["Rust core"]
        RUST["RRF fusion native"]
    end

    CHAT --> QUERY
    QUERY --> VECH
    QUERY --> BM25CH
    QUERY --> DKCH
    PDFVEC --> VECH
    BM25 --> BM25CH
    APPROVED --> DKVEC
    DKVEC --> DKCH
    VECH --> FUSION
    BM25CH --> FUSION
    RUST -->|RRF| FUSION

    DKCH --> CAND
    FUSION --> CAND
    CAND --> TUNE
    TUNE --> RERANK
    RERANK --> EVIDENCE

    TUNESTORE --> TUNE

    style SERVICES fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style STORE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style RETRIEVAL fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style KNOWLEDGE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style NATIVE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    class CHAT,QUERY,VECH,BM25CH,DKCH,FUSION,CAND,TUNE,RERANK,EVIDENCE,APPROVED node
    class PDFVEC,DKVEC,BM25,TUNESTORE storage
    class RUST native
```

**Feedback, review, and persistence path**

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"linear","nodeSpacing":35,"rankSpacing":45}}}%%
flowchart LR
    classDef node fill:#ffffff,stroke:#8c959f,stroke-width:1px,color:#24292f
    classDef storage fill:#f0fff4,stroke:#4ac26b,stroke-width:1px,color:#24292f

    subgraph SERVICES["Core Python services"]
        CHAT["chat service"]
        FEEDBACK["feedback entry"]
        FBANALYSIS["feedback analysis"]
        REVIEW["knowledge review"]
    end

    subgraph STORE["Local storage"]
        SQLITE["SQLite: sessions / index jobs"]
        TRACELOG["trace / logs: observability logs"]
        FEEDSTORE["feedback store: feedback records"]
        TUNESTORE["retrieval tuning store: tuning records"]
        DKSTORE["derived knowledge store"]
        DKVEC["Chroma derived-knowledge vectors"]
    end

    subgraph KNOWLEDGE["Feedback and review loop"]
        APPROVED["approved derived knowledge"]
    end

    CHAT --> SQLITE
    CHAT --> TRACELOG
    CHAT --> FEEDBACK
    FEEDBACK --> FEEDSTORE
    FEEDBACK --> FBANALYSIS
    FBANALYSIS --> REVIEW
    FBANALYSIS --> TUNESTORE
    REVIEW --> DKSTORE
    REVIEW --> APPROVED
    APPROVED --> DKVEC

    style SERVICES fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style STORE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    style KNOWLEDGE fill:#f6f8fa,stroke:#d0d7de,stroke-width:1px,color:#24292f
    class CHAT,FEEDBACK,FBANALYSIS,REVIEW,APPROVED node
    class SQLITE,TRACELOG,FEEDSTORE,TUNESTORE,DKSTORE,DKVEC storage
```

Summary builds a fixed-section structured summary of one named document; Compare builds a per-document profile across fixed dimensions and renders cited Markdown comparison blocks grouped by dimension. Both bind `[source:Pn]` citations deterministically from chunk metadata and run the same `validate_citations_native` checker as QA.

The Python layer owns orchestration, prompts, model clients, indexing, the CLI console, the standalone Debug console, and the FastAPI/Streamlit front ends. Approved derived knowledge is stored/reviewed in Python, indexed into Chroma, and searched as a separate QA evidence source; pending, stale, rejected, and archived entries do not enter retrieval. The Rust layer (`rust_core`) owns deterministic kernels that stay stable across agent changes and are unit-tested independently.

## Indexing Pipeline

Driven by `build_kb_index_transactional` whenever a KB's files change (`/add`, `/rm`, or the cloud upload/delete endpoints):

1. **Scan** — `scan_pdf_manifest_native` (Rust) hashes every PDF with rayon-parallel, 1 MiB-buffered SHA-256 and returns `{doc_id, documents: [{name, size, sha256}]}`, sorted by filename.
2. **Compare** — `manifests_match` reuses the index only if `doc_id`, `chunk_identity_version`, and every `{name, sha256}` match the saved manifest; any mismatch forces a rebuild.
3. **Parse** — `smart_parse` (PyMuPDF) extracts page text and reflows two-column layouts by block center-x. When optional OCR is enabled, low-text pages are rendered and recognized by local Tesseract within configured page-count and timeout budgets; otherwise they remain flagged as `is_ocr_fallback` and contribute only their native-text result.
4. **Chunk** — `chunk_paper` first detects conservative section spans, then keeps each child under 600 chars with 60-char overlap, preferring paragraph, sentence/semicolon, newline, and whitespace boundaries before falling back to a fixed window for very long unbroken text. The legacy 30-character minimum still filters unstructured noise, while every non-empty detected section or preamble is retained so structural boundaries cannot erase short evidence. Children never cross a detected section boundary. Each child stores a stable `parent_chunk_id`, section breadcrumb and within-parent order, plus up to 160 chars of section-bounded context; its own stable `chunk_id` and page span remain the citation identity.
5. **Index** — chunks land in Chroma (vector) and a persisted BM25 artifact that stores a compact chunk registry plus native `Bm25Index` bytes. Source name, section breadcrumb, locator context, and child body form the search text, while both stores round-trip the structural metadata and return the original child body. Loading restores the native index from bytes instead of rebuilding it from a Python tokenized corpus. `save_index_manifest` persists the manifest. Tokenization uses `tokenize_mixed_text_native` / `tokenize_corpus_native` (`jieba-rs` for Chinese, Snowball stemming + stopword removal for English).

Approved derived knowledge is indexed separately from source PDFs. Review actions can rebuild its Chroma collection, and stale scans mark knowledge whose document binding no longer matches the current KB documents.

**Chunk identity contract:**

```
chunk_id = sha256:{source_sha256}:src:{source_name}:p{page_start}-p{page_end}:c{local_chunk_index}
```

`chunk_id` is the single stable child identity key across chunker, index, retriever, RRF, citations, and evidence — dedup and fusion never rely on array position. `document_id = doc-{sha256(source-name-v1)}` is the stable per-KB ACL identity, while `parent_chunk_id = sha256:{source_sha256}:src:{source_name}:section:{section_index}` groups children for context hydration but never replaces their citation identity. The contract is versioned (`chunk_identity_version = source_sha256_name_page_span_local_v6_document_acl_parent_child_section_index_cs600_ov60_min30_ctx160`); changing document identity, chunk boundaries, structure detection, or indexed text must bump `CHUNK_IDENTITY_BASE_VERSION` so stale indexes rebuild instead of mixing schemes.

## Query Pipeline

- **Intent routing** — `RouterAgent` asks the LLM for structured `task_type ∈ {qa, summary, compare, unknown}` and falls back to a keyword rule on any parse error. All of `qa`, `summary`, and `compare` are wired to real subgraphs.
- **Rewrite + evidence-requirement planning** — `QueryRewriteAgent` emits 1–3 keyword queries plus at most three atomic drafts shaped as `{question, retrieval_query, recovery_query}`; the server assigns stable `r1..r3` IDs and falls back to one original-question requirement on empty/failed planning. `RewriteVerifyAgent` uses one embedding batch for two semantic guards: each requirement question is compared with the original question plus recent history, then its primary/recovery queries are compared with that requirement. A drifting requirement is dropped, a drifting focused query falls back to its requirement question, and an all-dropped plan falls back to the original requirement. The existing rewrite kept/dropped behavior and `steps_trace` remain intact.
- **Query-level RRF with provenance** — every original, rewritten, and requirement query searches the hybrid PDF engine and approved derived knowledge. Each query/channel ranking contributes equally through `score(d) = Σ_q,c 1 / (k + rank_q,c(d))` (`k = 60`), candidates are deduplicated by stable `chunk_id`, and ties break by identity key. Fused metadata records matched queries, channels, requirement IDs, hit count, original-query participation, best rank, and retrieval round instead of collapsing provenance to one rewrite.
- **Bounded Parent–Child hydration** — after child-level reranking and support decisions, each source hit loads a contiguous, balanced window of children sharing its `parent_chunk_id`, bounded independently by chunk and character budgets. Added siblings keep their own IDs/pages and carry `context_anchor_chunk_id` plus `context_expansion=section`; derived knowledge is never expanded. Missing/incomplete structure and disabled parent hydration use the legacy neighbor path, so old indexes remain readable while the version gate schedules new indexes for rebuild.
- **Query-aware extractive evidence span** — before the global pack budget is applied, each canonical long chunk is reduced to one continuous, verbatim source interval selected from query and requirement-term overlap; no paraphrasing or synthetic joining is allowed. `evidence_span_start` / `evidence_span_end` are 0-based, half-open offsets into the ultimate child text. If no match is reliable, selection fails open to the complete available body. The isolated model view removes `meta.context` so facts outside the verified span cannot re-enter through rendering. Adaptive retrieval may reselect from a private local source copy, but that copy is excluded from API and trace payloads.
- **Deterministic Evidence Pack** — hydrated anchors, requirement-attributed candidates, adaptive-retrieval carryovers, and sibling context are reduced to one immutable QA evidence closure under global document and character budgets. The character budget equals the exact rendered QA generation-evidence context—including document/knowledge tags, identity attributes, locator headers, materialized text, and block separators—while excluding system instructions, conversation history, and the query; it does not pretend to be a model-token estimate. Anchors and verified carryovers are hard requirements: if they alone exceed either budget, QA fails closed instead of silently dropping them. The evidence verifier (which may select a subset), answer generator, and claim audit can only consume chunks from this same closure. Exact overlap between consecutive children is removed only in the isolated packed copy; `retrieval.evidence_text_start`, `retrieval.evidence_text_end`, and `retrieval.evidence_trimmed_overlap_chars` preserve its source-text range and trimming provenance.
- **Requirement quota + rerank** — before `BGEReranker` (`bge-reranker-v2-m3`), a bounded candidate selector reserves at least one attributed candidate per requirement, then fills the remaining budget in fused order. Evidence-verifier candidates use the same requirement-first rule before source diversification, preventing a strong first query from starving later requirement hits. Final reranking still scores `(original_query, doc)` and rewrites never directly bias the cross-encoder score.
- **Closed-set evidence check + bounded adaptive recovery** — when evidence verification is enabled, eligible exact-fact questions and all multi-requirement questions must clear one assessment per requirement (`supported`, `missing`, or `contradictory`) against only the supplied requirement IDs and chunk IDs before generation. Missing/duplicate/unknown requirement IDs, fabricated chunks, unsupported requirements, and ungrounded contradictions cannot pass. If the gap is recoverable, CogDoc makes one retry by default: missing requirements' `recovery_query` values are prioritized immediately after the original query, retrieval depth grows by a capped multiplier, and the fused/reranked evidence is checked again. Retry count, query budget, and `top_k` are all bounded; verifier errors do not retry. If every requirement still lacks valid support, generation is skipped with a stable fail-closed refusal.
- **Attributed feedback weights** — positive feedback (thumbs-up or an above-neutral rating) may boost its cited/evidence chunks. Thumbs-down, corrections, and below-neutral ratings create a negative retrieval weight only when explicitly classified as `feedback_type=bad_retrieval`; other answer-quality failures do not punish potentially correct evidence. `skip_retrieval_feedback=true` suppresses both positive and negative tuning for that entry.
- **Generation + citation self-heal** — `Generator` (OpenAI-compatible; cloud `deepseek-chat` or local `qwen2.5:7b`, `temperature = 0.2`) wraps docs as `<Document source=… page=… chunk_id=…>` and forces `[source:Pn]` tags. `validate_citations_native` (Rust) returns structured `missing_citations` / `invalid_sources` / `invalid_pages`; `citation_node` turns failures into a critique and loops `generate → citation` up to `max_iteration_count` (default `2`). Only physically validated answers leave the task subgraphs.
- **Parent-graph claim audit and bounded repair (opt-in)** — with `CLAIM_VERIFICATION_ENABLED=true`, QA, Summary, and Compare outputs enter `claim_audit_node` after their physical citation checks. `ClaimEvidenceVerifierAgent` splits the candidate into factual claims, batches them, and labels each `supported`, `unsupported`, or `insufficient` using only the evidence explicitly cited by that claim. A failure enters `claim_repair_node`; the revised answer must pass the deterministic citation checker and then a fresh semantic audit. Repair attempts are capped by `CLAIM_VERIFICATION_MAX_REPAIR_ATTEMPTS` (default `1`). A verifier error, repair error, invalid repaired citation after the limit, or exhausted semantic audit fails closed through `claim_block_node`, which replaces the candidate with a stable refusal and clears its citations/evidence. The semantic gate is disabled by default so deployments can establish a reviewed baseline before enabling it.

  QA, Summary, and Compare always buffer candidate model tokens because their intermediate text can contain internal Evidence IDs and has not yet passed final rendering. Enabling this gate preserves the same buffering rule for any other routed candidate it audits. Node progress events still stream; once parent post-processing completes (pass, an intentional `not_run`, or fail-closed refusal), the finalized answer is emitted as one token event followed by the normal `final` event.

**Summary subgraph** — `document_loader` selects one named document (or the only document in the corpus; ambiguous multi-document queries get an actionable message), `section_planner` fixes the sections to background/goals, solution/process, rules/requirements, value/output, and limitations/notes unless custom titles are supplied in state, `section_summary` writes one short paragraph per section (model writes prose only; `[source:Pn]` tags are bound deterministically from the chunks it used), and `global_summary` assembles the answer and re-runs the citation checker. No-evidence sections carry no citation and no evidence.

**Compare subgraph** — `document_loader` requires at least two explicitly named documents; local Ollama mode caps this at two documents. `document_profile` builds a per-document profile across fixed dimensions (cloud: method / data / metrics / strengths / limitations / scenarios; local: method / data / metrics / limitations) reusing the Summary cell primitive. `compare_table` renders Markdown comparison blocks; cloud mode also asks for a short guarded conclusion, while local mode skips that extra call to reduce memory pressure. `compare_citation_node` validates the conclusion independently and then the comparison blocks; any failure downgrades to the bare blocks with a warning. An all-no-evidence comparison is not flagged as missing citations.

## Native Core

`rust_core` is a PyO3/maturin extension, loaded via `tools.rust_core_loader.ensure_rust_core`, which fails fast with a `maturin develop` hint if the build is missing or a symbol is stale. It exposes six native symbols, all listed in `scripts/check_native.py` so `make check` fails against a stale build.

| Symbol | Module | Purpose |
| --- | --- | --- |
| `scan_pdf_manifest_native` | `scanner.rs` | Rayon-parallel, buffered SHA-256 of every PDF; size + hash manifest, stably sorted |
| `rrf_fusion_native` | `rrf.rs` | Deterministic RRF (`k=60`) merge of vector + BM25 results, keyed on `chunk_id` |
| `validate_citations_native` | `citation.rs` | Structured citation check → `invalid_sources` / `invalid_pages` / `missing_citations` |
| `tokenize_mixed_text_native` | `tokenizer.rs` | Mixed CN/EN tokenizer: `jieba-rs` for Chinese, Snowball stemming + stopword removal for English (identifiers/versions kept verbatim); token-for-token aligned with a Python reference |
| `tokenize_corpus_native` | `tokenizer.rs` | Batch corpus tokenizer used by BM25 indexing to avoid Python-side per-document tokenization loops |
| `Bm25Index` (class) | `bm25.rs` | BM25 index + `score_topk` + native bytes persistence, bit-aligned with `rank_bm25.BM25Okapi`; top-k selected natively |

## Project Layout

```text
CogDoc/
├── src/cogdoc/
│   ├── cli.py
│   ├── debug.py
│   ├── agents/
│   ├── api/
│   │   └── routes/
│   ├── config/
│   ├── frontend/
│   ├── graph/
│   │   └── subgraphs/
│   ├── observability/
│   ├── service/
│   └── tools/
│       └── retriever/
├── rust_core/src/
├── scripts/
├── tests/
├── eval/
├── docs/
└── pyproject.toml
```

| Path | Responsibility |
| --- | --- |
| `src/cogdoc/cli.py` | Multi-KB, multi-conversation CLI entry point (`python -m cogdoc.cli` / `cogdoc`) |
| `src/cogdoc/debug.py` | Standalone Trace Debug console (`python -m cogdoc.debug` / `cogdoc-debug`) |
| `src/cogdoc/agents/` | Routing, query rewrite, generation, citation validation, feedback understanding, and Summary / Compare agent primitives |
| `src/cogdoc/api/` | FastAPI app, routes, schemas, persistence, access control, metrics, feedback / knowledge stores, webhooks |
| `src/cogdoc/frontend/` | Streamlit thin client and API client |
| `src/cogdoc/graph/` | LangGraph state, main workflow, and QA / Summary / Compare subgraphs |
| `src/cogdoc/service/` | Chat / ingest services, KB lifecycle, transactional indexing, locks, cleanup, and background work |
| `src/cogdoc/tools/` | PDF parsing, chunking, manifests, embedding, rerank, Rust loading, and retrievers |
| `rust_core/src/` | PyO3 native core: scanner, tokenizer, BM25, RRF, citation validator |
| `scripts/`, `tests/`, `eval/`, `docs/` | Health-check scripts, tests, offline eval sets, and project docs |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `COGDOC_DOC_DIR` | `your_documents` | Inbox directory of PDFs that `/add` ingests into a KB |
| `COGDOC_DATA_DIR` | `./data` | Root for persisted KB state, SQLite DBs, manifests, and index artifacts |
| `COGDOC_TRACE_ENABLED` | `true` | Enable JSON trace export for request inspection |
| `COGDOC_TRACE_DIR` | `logs/traces` | Directory for exported trace JSON files |
| `COGDOC_WEBHOOK_URL` | unset | Optional endpoint for pending knowledge review events |
| `COGDOC_WEBHOOK_SECRET` | unset | Optional shared secret sent with webhook requests |
| `COGDOC_WEBHOOK_TIMEOUT_SECONDS` | `3` | Timeout for webhook delivery attempts |
| `COGDOC_FEEDBACK_STORE` | `jsonl` | Feedback storage backend; set `sqlite` to use SQLite with JSONL export |
| `COGDOC_DERIVED_KNOWLEDGE_INDEX_AUTO_REFRESH` | `false` | Rebuild approved derived-knowledge vectors in the background after review changes |
| `COGDOC_ACCOUNT_AUTH_ENABLED` | `false` | Enable persistent human accounts, login sessions, workspaces, invitations, and resource ACL enforcement; opt-in for compatibility |
| `COGDOC_SELF_REGISTRATION_ENABLED` | `true` | Permit public account registration while account auth is enabled; disable after enterprise bootstrap to use invitations only |
| `COGDOC_AUTH_SESSION_TTL_SECONDS` | `2592000` | Login-session lifetime (30 days) |
| `COGDOC_AUTH_INVITE_TTL_SECONDS` | `604800` | One-time workspace-invitation lifetime (7 days) |
| `COGDOC_AUTH_MAX_FAILED_LOGINS` | `5` | Consecutive bad password attempts before temporary lockout |
| `COGDOC_AUTH_LOCKOUT_SECONDS` | `900` | Account lockout duration after the failed-login limit is reached |
| `COGDOC_API_KEYS` | unset | Comma-separated legacy default/admin API keys; auth is open only when this, principals, and review keys are all empty |
| `COGDOC_API_PRINCIPALS` | unset | One-line JSON map from API keys to `tenant_id`, `subject_id`, and RBAC `role`; preferred for team workspaces |
| `COGDOC_EVAL_REVIEW_API_KEYS` | unset | Legacy evidence-eval/Research review keys; each also authenticates ordinary routes as default/admin |
| `RATE_LIMIT_PER_MINUTE` | `120` | Token-bucket refill rate for protected API routes |
| `RATE_LIMIT_BURST` | `120` | Token-bucket burst capacity; `<=0` disables rate limiting |
| `COGDOC_TENANT_MAX_KNOWLEDGE_BASES` | `0` | Hard KB limit per workspace; `0` is unlimited |
| `COGDOC_TENANT_MAX_DOCUMENTS` | `0` | Hard committed + in-flight PDF limit per workspace; `0` is unlimited |
| `COGDOC_TENANT_MAX_STORAGE_MB` | `0` | Hard committed + in-flight PDF-byte limit per workspace in MiB; `0` is unlimited |
| `COGDOC_MAX_UPLOAD_MB` | `50` | Maximum PDF upload size through the API/frontend |
| `COGDOC_RESEARCH_WORKERS` | `2` | Maximum concurrent background research evidence/report jobs |
| `COGDOC_CHAT_STREAM_IDLE_TIMEOUT_SECONDS` | `300` | Maximum idle time between SSE worker events; prevents indefinitely open streams |
| `COGDOC_RESEARCH_RETRIEVAL_TOP_K` | `8` | Candidate depth retrieved and reranked per research section |
| `COGDOC_RESEARCH_MAX_PENDING` | `32` | Maximum admitted queued/running Research background attempts before API requests fail with `503` |
| `COGDOC_RESEARCH_PROVIDER_WORKERS` | `4` | Maximum concurrent process-isolated provider calls within background Research attempts |
| `COGDOC_RESEARCH_PROVIDER_MAX_PENDING` | `16` | Maximum admitted running/queued provider calls across background Research attempts |
| `COGDOC_RESEARCH_PROVIDER_CALL_TIMEOUT_SECONDS` | `180` | Per-call provider ceiling, further contracted by the remaining phase or planning deadline |
| `COGDOC_RESEARCH_LLM_PROCESS_ISOLATION_ENABLED` | `true` | Require recognized standard `ChatOpenAI` Research calls to fail closed if they cannot enter process isolation |
| `COGDOC_RESEARCH_PROVIDER_KILL_GRACE_SECONDS` | `0.5` | Grace period after terminating an isolated provider child before escalating to kill |
| `COGDOC_RESEARCH_PROVIDER_IPC_MAX_BYTES` | `2000000` | Maximum serialized result envelope accepted from an isolated provider child |
| `COGDOC_RESEARCH_EVIDENCE_DEADLINE_SECONDS` | `900` | Durable wall-clock deadline for one evidence attempt |
| `COGDOC_RESEARCH_REPORT_DEADLINE_SECONDS` | `1800` | Durable wall-clock deadline for one report-generation attempt |
| `COGDOC_RESEARCH_PLANNING_DEADLINE_SECONDS` | `300` | Wall-clock deadline for one automatic Research planning request |
| `COGDOC_RESEARCH_PLANNING_WORKERS` | `1` | Dedicated automatic-planning workers, isolated from the shared API offload pool |
| `COGDOC_RESEARCH_PLANNING_MAX_PENDING` | `8` | Maximum admitted running/queued automatic-planning requests before `503` |
| `COGDOC_RESEARCH_MAX_RETRIEVAL_QUERIES` | `128` | Per-attempt retrieval-query budget reserved before retrieval |
| `COGDOC_RESEARCH_MAX_CANDIDATE_DOCS` | `2048` | Per-attempt candidate-document budget reserved before retrieval |
| `COGDOC_RESEARCH_MAX_LLM_CALLS` | `256` | Per-attempt structured/model-call budget |
| `COGDOC_RESEARCH_MAX_MODEL_INPUT_CHARS` | `5000000` | Per-attempt aggregate model-input character budget |
| `LLM_RESEARCH_PLANNER_BACKEND` | `default` | Research-planning backend: follow the request, force `cloud`, or force `local` |
| `LLM_RESEARCH_PLANNER_MODEL_NAME` | unset | Optional cloud model override for editable research-plan generation |
| `OLLAMA_RESEARCH_PLANNER_MODEL_NAME` | unset | Optional local model override for editable research-plan generation |
| `QA_PARENT_CONTEXT_ENABLED` | `true` | Hydrate reranked child hits with bounded siblings from the same detected section; `false` keeps legacy neighbor expansion |
| `QA_PARENT_CONTEXT_MAX_CHUNKS` | `5` | Maximum child chunks retained per structural parent window, including the anchor |
| `QA_PARENT_CONTEXT_MAX_CHARS` | `3600` | Soft character budget per structural parent window; the anchor is never dropped |
| `QA_EVIDENCE_SPAN_ENABLED` | `true` | Select one query-aware, verbatim interval from each long canonical chunk before Evidence Pack budgeting |
| `QA_EVIDENCE_SPAN_MAX_CHARS_PER_DOC` | `420` | Maximum selected body characters per chunk; unreliable matches fail open to the full available body |
| `QA_EVIDENCE_SPAN_CONTEXT_SENTENCES` | `1` | Maximum neighboring sentences retained on each side of the selected evidence sentence |
| `QA_EVIDENCE_PACK_MAX_DOCS` | `8` | Global chunk limit for the immutable QA model evidence payload; anchors and verified carryovers remain hard requirements |
| `QA_EVIDENCE_PACK_MAX_CHARS` | `7200` | Exact rendered generation-evidence limit, including tags/IDs/locators/text/separators but excluding system/history/query |
| `QA_ABSTAIN_ENABLED` | `true` | Return a deterministic no-evidence answer before LLM generation when retrieval confidence is low |
| `QA_ABSTAIN_MAX_VECTOR_DISTANCE` | `0.86` | Maximum accepted normalized-vector L2 distance |
| `QA_ABSTAIN_MIN_BM25_SCORE` | `10.0` | BM25 score that can independently establish retrieval support |
| `QA_ABSTAIN_MIN_KNOWLEDGE_SCORE` | `0.5` | Minimum support score for approved derived knowledge |
| `QA_EVIDENCE_VERIFY_ENABLED` | `true` | Verify exact-fact questions against retrieved chunks before answer generation |
| `QA_EVIDENCE_VERIFY_MAX_DOCS` | `3` | Maximum source-diversified chunks sent to the evidence verifier |
| `QA_EVIDENCE_VERIFY_MAX_CHARS_PER_DOC` | `1600` | Per-chunk text limit for evidence verification |
| `QA_EVIDENCE_VERIFY_BORDERLINE_MIN_SCORE` | `0.75` | Minimum first-stage support score eligible for verifier rescue |
| `QA_RETRIEVAL_MAX_QUERIES` | `7` | Per-round cap across original, rewritten, requirement, and recovery queries after normalization/deduplication |
| `QA_ADAPTIVE_RETRIEVAL_ENABLED` | `true` | Allow bounded recovery retrieval for incomplete requirement evidence |
| `QA_ADAPTIVE_RETRIEVAL_MAX_RETRIES` | `1` | Maximum recovery rounds (`0` disables retries; validated range `0..2`) |
| `QA_ADAPTIVE_RETRIEVAL_TOP_K_MULTIPLIER` | `2.0` | Retrieval-depth multiplier applied on each recovery round |
| `QA_ADAPTIVE_RETRIEVAL_MAX_TOP_K` | `36` | Hard `top_k` ceiling after adaptive depth expansion |
| `CLAIM_VERIFICATION_ENABLED` | `false` | Enable the post-generation claim-level semantic gate; enabled mode fails closed |
| `CLAIM_VERIFICATION_MAX_CLAIMS` | `40` | Maximum auditable claim fragments per answer; overflow is not silently released |
| `CLAIM_VERIFICATION_MAX_CLAIMS_PER_BATCH` | `8` | Maximum claims sent in one verifier call |
| `CLAIM_VERIFICATION_MAX_DOCS_PER_BATCH` | `12` | Maximum evidence chunks visible to one verifier/repair call |
| `CLAIM_VERIFICATION_MAX_CHARS_PER_DOC` | `1600` | Per-evidence-chunk character limit for claim verification and repair |
| `CLAIM_VERIFICATION_MAX_REPAIR_ATTEMPTS` | `1` | Bounded repair attempts before the answer is rejected (`0` disables repair) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Local OpenAI-compatible Ollama endpoint |
| `OLLAMA_MODEL_NAME` | `qwen2.5:7b` | Local model name |
| `OLLAMA_TIMEOUT_SECONDS` | `180` | Local model request timeout |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | Cloud OpenAI-compatible endpoint |
| `LLM_MODEL_NAME` | `deepseek-chat` | Cloud model name |
| `LLM_API_KEY` | `your-cloud-api-key-here` | Cloud API key |
| `LLM_TIMEOUT_SECONDS` | `90` | Cloud model request timeout |
| `LLM_<NODE>_BACKEND` | `default` | Per-node backend: `default`, `cloud`, or `local` |
| `LLM_<NODE>_MODEL_NAME` | unset | Per-node cloud model override |
| `OLLAMA_<NODE>_MODEL_NAME` | unset | Per-node local model override |
| `HF_TOKEN` | unset | Optional Hugging Face Hub token |

Standard factory-built `ChatOpenAI` calls used by automatic Research planning and by evidence/report work are reconstructed in fresh spawned child processes. A timeout, durable pause/cancel, or an application-shutdown signal terminates, escalates to kill after the configured grace period, joins, and reaps the local child. `make serve` configures a finite Uvicorn graceful-request shutdown ceiling so active HTTP planning reaches that application signal; custom launchers must configure an equivalent bound. Opaque or nonstandard clients retain a bounded compatibility path with cooperative cancellation; a client recognized as `ChatOpenAI` fails closed when isolation is required but a safe child recipe cannot be built. Killing the local client process cannot guarantee cancellation at an already-contacted remote API or Ollama server, which may continue computation or billing. Retrieval, reranking, embedding, Hugging Face model loading, Torch, and native/Rust work remain in-process and cannot yet be forcibly preempted by this provider isolation layer.

`<NODE>` can be `ROUTER`, `QUERY_REWRITER`, `SOURCE_RESOLVER`, `EVIDENCE_VERIFIER`, `CLAIM_VERIFIER`, `CLAIM_REPAIRER`, `QA_GENERATOR`, `SUMMARY_GENERATOR`, `COMPARE_PROFILE`, or `COMPARE_CONCLUSION`. For independent post-generation review, for example, keep answer generation on cloud while setting `LLM_CLAIM_VERIFIER_BACKEND=local` and `OLLAMA_CLAIM_VERIFIER_MODEL_NAME=<review-model>`; to repair locally too, set `LLM_CLAIM_REPAIRER_BACKEND=local` and `OLLAMA_CLAIM_REPAIRER_MODEL_NAME=<repair-model>`. The corresponding cloud model overrides are `LLM_CLAIM_VERIFIER_MODEL_NAME` and `LLM_CLAIM_REPAIRER_MODEL_NAME`. Citation syntax and source/page membership remain deterministically validated by Rust; the claim verifier adds the optional model-based semantic support decision.

Requirements: Python 3.11+ (developed on 3.13; the extension targets 3.8+), a Rust toolchain with `cargo` (edition 2024, via [rustup](https://rustup.rs/)), and [maturin](https://www.maturin.rs/). Optional: [Ollama](https://ollama.com/) for local models. See `.env.example` for the full set of tunables (retrieval `top_k`, rerank `top_n`, RRF `k`, CUDA memory floors, eval set paths).

## Development & Testing

| Command | Description |
| --- | --- |
| `make native` | Build / rebuild `rust_core` (required after editing `.rs`) |
| `make check` | Verify the extension is importable and all native symbols exist |
| `make test` | Run the Python test suite |
| `make smoke-api` | Run an in-process API smoke test without real LLM/index work |
| `make backup` | Back up local runtime state under `backups/` |
| `make eval` | Run offline retrieval evaluation (`recall@k`, MRR) |
| `make eval-coverage` | Check retrieval eval coverage without running real retrieval |
| `make eval-retrieval-report` | Run the 100-query retrieval profile and write its report |
| `make eval-retrieval-baseline` | Generate the reviewed real-retrieval baseline |
| `make eval-retrieval-gate` | Enforce absolute thresholds and compare with the retrieval baseline |
| `make eval-quality` | Run offline quality evaluation (router, citations, faithfulness ledger) |
| `make eval-quality-coverage` | Run quality metrics and enforce coverage dimensions |
| `make eval-suite` | Run the combined eval gate (coverage audits + quality metrics) |
| `make eval-suite-run-retrieval` | Run the combined eval suite and execute real retrieval metrics |
| `make eval-suite-report` | Write `eval/eval_suite_report.json` |
| `make eval-suite-baseline` | Compare against `eval/eval_suite_baseline.json` |
| `make eval-suite-update-baseline` | Refresh `eval/eval_suite_baseline.json` after review |
| `make run` | Start the interactive CLI console |
| `make serve` | Start the FastAPI service (`uvicorn cogdoc.api.app:app`) |
| `make frontend` | Load `.env` without overriding exported values, then start the Streamlit web app |
| `make debug` | Start the standalone Debug console |
| `uvicorn scripts.cogeval_cogdoc_wrapper:app --port 8003` | Start the optional CogEval-compatible adapter for a running CogDoc API |
| `cd rust_core && cargo test` | Run Rust unit tests |
| `cd rust_core && cargo fmt --check` | Check Rust formatting |

Test layering: business logic and the Python↔native API contract are tested in Python (`tests/`); pure-Rust logic uses Rust `#[test]` in `rust_core/src/`. Native-dependent Python tests `importorskip` when `rust_core` is not built, so run `make native` before a full regression.

GitHub CI runs Rust formatting/tests, builds and runtime-checks the native wheel, runs the full Python suite plus API smoke, the production account-auth smoke, and the lightweight eval gate. It then builds and boots the Docker image as a non-root user and repeats the real account flow: readiness, anonymous denial, registration/login, workspace invitation, viewer write denial, and private-KB grant/revocation; after a graceful stop it also exercises the volume-backed default backup, read-only verification, and a root-helper restore drill. Unfiltered `pytest` discovery means the account store/routes, workspace isolation, resource-policy/grant, pre-top-k scope, and Research revalidation tests are included automatically. Third-party Actions are pinned to full commits and jobs use read-only repository permissions. Tags and manual dispatches also build a recursively checksummed bundle containing the Python sdist/wheel, the Linux x86_64 CPython 3.13 native wheel, and root-preserving `scripts/` operations helpers; before uploading it, the workflow checks version/tag agreement, reruns the same full Python/smoke/eval gates, force-installs both wheel and sdist bundles plus their native wheel in separate environments, and repeats that Docker account/backup/restore smoke. This artifact workflow does not publish to PyPI or GitHub Releases, and the two wheels must be installed together.

Offline evaluation uses local JSONL files under `eval/`. `make eval-suite` is the default lightweight gate: it audits retrieval and quality eval coverage, runs quality metrics, prints summaries by case type and layer, and skips model-backed retrieval by default. `make eval-suite-report` writes `eval/eval_suite_report.json`; `make eval-suite-baseline` compares aggregate, case-type, and layer-level quality metrics against `eval/eval_suite_baseline.json`; `make eval-suite-update-baseline` refreshes that baseline after review. Generated reports and baselines are ignored by Git.

The real-retrieval profile expects at least 100 reviewed queries in `eval/retrieval_eval.jsonl`: 40 single-source, 20 multi-source, 20 hard, and 20 no-answer. The baseline coverage profile additionally requires 20 valid evidence-plan queries, 20 gold-requirement queries, 20 chunk-gold queries, 10 span-gold queries, and 10 hard-negative queries. Counts are per independent query, not per requirement inside a query. Evidence metrics always report their true denominators, but remain out of the baseline—and absolute gates fail closed—until the configured sample minimum is met. `make eval-retrieval-baseline` records the reviewed reference run, while `make eval-retrieval-gate` compares relevance metrics with that baseline and enforces the absolute limits in the local `eval/retrieval_gate.json`; use `eval/retrieval_gate.example.json` as its schema. Reports include aggregate and per-layer MRR/Recall/Hit metrics, mean and P95 latency, and a separately reported warmup that is excluded from steady-state latency. `answerable_acceptance_rate` and `no_answer_abstention_rate` measure the deterministic first-stage evidence gate directly. Exact-fact questions that pass that gate, plus borderline candidates above `QA_EVIDENCE_VERIFY_BORDERLINE_MIN_SCORE`, receive a second structured evidence-sufficiency check before generation. No-answer rows also report `no_answer_false_positive@k`; that metric only says whether the retriever returned candidates, not whether either gate accepted them or the generated answer was false. The default distance/BM25 thresholds were calibrated on the local reviewed set and should be recalibrated when the corpus or embedding model changes.

### Requirement-aware retrieval eval data

Retrieval JSONL rows retain `query`, `expected_sources`, and optional `doc_id` / `layer`, and may now add the following fields:

- `rewritten_queries`: optional reviewed rewrite inputs used by the real retrieval run.
- `evidence_requirements`: up to three runtime query plans with `requirement_id`, `question`, `retrieval_query`, and `recovery_query`. These drive requirement-attributed retrieval and the bounded recovery path; `--verify-evidence` additionally runs the structured closed-set assessment.
- `gold_requirements`: evaluator-only ground truth. Each row names a `requirement_id` and at least one `acceptable_chunk_ids` or `acceptable_sources` list; chunk annotations are preferred because a hit on the right PDF but wrong block should not count as evidence coverage. It may additionally define alternative `acceptable_spans` as `{chunk_id, start, end}`; offsets are 0-based, half-open positions in the canonical child text before overlap/span trimming.
- `hard_negative_chunk_ids`: optional known distractor chunks used to measure rejection.

One formatted JSONL object (write it on one physical line in the dataset) looks like this:

```json
{
  "id": "policy-dates-and-fees",
  "query": "What are the deadline and fee?",
  "doc_id": "policy",
  "layer": "multi-source",
  "expected_sources": ["dates.pdf", "fees.pdf"],
  "rewritten_queries": ["application deadline", "application fee"],
  "evidence_requirements": [
    {"requirement_id": "r1", "question": "What is the deadline?", "retrieval_query": "application deadline", "recovery_query": "submission closing date"},
    {"requirement_id": "r2", "question": "What is the fee?", "retrieval_query": "application fee", "recovery_query": "registration cost"}
  ],
  "gold_requirements": [
    {"requirement_id": "r1", "acceptable_chunk_ids": ["deadline-chunk"], "acceptable_spans": [{"chunk_id": "deadline-chunk", "start": 120, "end": 168}]},
    {"requirement_id": "r2", "acceptable_chunk_ids": ["fee-chunk"]}
  ],
  "hard_negative_chunk_ids": ["old-policy-chunk"]
}
```

When annotations are present, reports add `requirement_recall@k`, `all_requirements_covered@k`, and binary-relevance `evidence_ndcg@k`; chunk-level gold also enables `chunk_precision@k`, while hard negatives enable `hard_negative_rejection@k`. `generation_requirement_coverage` measures the same gold requirements against the actual bounded Parent–Child context prepared for generation. Verifier runs add `requirement_full_coverage_rate`; adaptive runs add `adaptive_retry_trigger_rate` and, for retried rows, `adaptive_rescue_rate`. `retrieval_query_count`, `parent_context_trigger_rate`, and the parent/neighbor expansion counts expose rollout cost and structural-index coverage. Evidence-span retained-character ratio, fallback rate, and pre/post gold-span recall are rollout diagnostics and are never part of the default gate. Trigger/count metrics are likewise excluded; evidence coverage, ranking, rejection, and rescue metrics can be baseline-gated when present.

Each report row also records `retrieved_items`, `generation_context_items`, `evidence_requirement_assessments`, `missing_evidence_requirement_ids`, `retrieval_retry_count`, `adaptive_retrieval_rescued`, `retrieval_query_count`, `retrieval_ranking_count`, `retrieval_carryover_count`, parent/neighbor expansion counts, and per-channel counts in `retrieval_channel_counts`, so a regression can be traced to planning, fusion, structure hydration, verification, or recovery rather than inferred from a final source list. Full QA traces additionally record the expansion counts, and evidence previews retain section identity and context-attribution metadata for rollout comparison.

`make eval` runs an ad hoc retrieval check against the local set and falls back to `eval/retrieval_eval.example.jsonl` on a clean checkout. `make eval-coverage` checks the smoke profile without touching the index. Run `make eval-suite-run-retrieval` when the combined suite should also execute real retrieval. `make eval-quality` measures router accuracy, citation accuracy, and the manual faithfulness ledger across QA, Summary, Compare, multi-turn, no-answer, and feedback layers; `make eval-quality-coverage` additionally enforces the required case types and recommended layers. Thumbs-down and correction feedback writes `eval_draft` rows to `bad_cases.jsonl`, so reviewed cases can be promoted into the quality eval set. For a coverage-only quality check, run `python scripts/eval_quality.py --coverage-only`. `--coverage-only` is intentionally incompatible with `--check-coverage`, `--json`, and `--baseline`.

Quality cases can also carry runtime `claim_audit` data directly, under `output.claim_audit`, or under `trace.output.claim_audit`. The report recomputes claim support, citation coverage, unsupported/insufficient rates, repair success, audit observability, and verifier latency from claim details; it does not trust precomputed counts, and these rollout diagnostics are intentionally not part of the default baseline gate. The general scoring layer also accepts a deterministic `claim_audit_assertion`; missing audit evidence is `NOT_OBSERVABLE`, while configurable support/citation/status thresholds can be promoted to a strict gate after domain calibration.

Run `python scripts/eval_retrieval.py --rerank --verify-evidence` to include the cloud evidence verifier in final acceptance/abstention metrics; add `--local-verifier` to use Ollama. This mode makes model calls and is intentionally excluded from the default retrieval gate.

### v7 migration and four-route calibration

Use `python scripts/migrate_v7_indexes.py scan` before rollout, then `run` to rebuild stale KBs transactionally while retaining the previous generation. A recorded run can be reverted with `rollback <run_id>` and, after acceptance, its retained generations can be removed with `finalize <run_id>`.

Authorized reviewers can run the same single-flight workflow through `/v1/index-migrations` or the Evidence Review Desk's generation control. Tenant filtering is enforced and physical storage IDs are never returned. `make eval-multi-route` and `make calibrate-multi-route` expose the evaluation and calibration workflow as standard development targets.

Run `scripts/eval_multi_route_retrieval.py` to produce full, per-route, and leave-one-out evaluations with layered ranking, coverage, abstention, and latency metrics. Feed that report to `scripts/calibrate_multi_route_retrieval.py` to generate a versioned, rollbackable recommendation for route weights, top-k, per-route quota, and abstention thresholds. The evaluator and calibrator write artifacts only; they never change live settings. The Evidence Review Desk in the web UI now includes a retrieval path console for raw routes, RRF contributions, rerank movement, gate decisions, and review-gated human labels.

Every chat request gets a `request_id`/`trace_id`. When `COGDOC_TRACE_ENABLED=true`, the service writes JSON traces under `COGDOC_TRACE_DIR` (default `logs/traces`), and the same safe payload is available through `GET /v1/traces/{trace_id}`. `GET /v1/traces` lists recent traces and can be scoped by `doc_id` and `session_id`, which is how the Streamlit Trace panel shows only the current conversation. Trace files include `schema_version`, `status` (`ok`, `degraded`, or `failed`), total `duration_ms`, a safe config snapshot, step summaries, rewrite summaries, error summaries, and only truncated evidence previews rather than full document text. QA rerank steps additionally expose Evidence Pack input/kept/dropped counts and characters, overlap removed, drop-reason counts, anchor/pinned counts, and the hard-constraint `over_budget` decision. The standalone Debug console reads the same trace format.

Backup/restore and index rebuild rules are covered in [PRODUCTION_zh-CN.md](docs/PRODUCTION_zh-CN.md).

## Known Limitations

- **OCR is an opt-in Tesseract MVP.** It is disabled by default, supports locally installed language packs, and intentionally has no hosted provider. Recognition quality depends on scan quality, selected languages, and DPI.
- Summary and Compare are fixed-schema MVPs; cloud mode runs independent section/dimension LLM cells concurrently with stable output order, while local Ollama mode stays serial to avoid memory pressure. The default section/dimension sets are fixed unless passed through graph state.
- Research reports support editable AI planning, atomic evidence requirements, closed-set verification, deterministic citations, mandatory section-local claim and requirement-coverage auditing with one shared bounded repair, per-section review, explicit gap acceptance, bounded version history, selective regeneration, and immutable publication. Atomic requirements are the machine-enforced completion contract; free-form `success_criteria` remains a human review note. Selective regeneration only retrieves, verifies, and rewrites `changes_requested` or legacy unaudited sections; approved sections and accepted gaps are preserved while the global public citation ledger is rebuilt and validated. A frozen provenance contract—including retrieval/verification settings and model routing—blocks stale evidence from generation, review, and publication until an explicit full refresh archives the old report. A local Research job is a privacy constraint: planning, evidence verification, writing, claim/coverage audit, and repair all reject cloud node overrides.
- Published v2 artifacts hash the exact Markdown, citation ledger, trackable provenance, bounded `verification.json` claim/coverage summaries, evidence identity/hash commitments, version, and generation timestamp. The deterministic ZIP also carries per-file hashes. Legacy Markdown is served only as `legacy-unverified` and cannot produce a bundle; malformed or tampered current/published bodies are withheld from list, detail, and download responses.
- Local Compare intentionally supports only two documents, uses four core dimensions, and skips the extra conclusion generation step to reduce Ollama memory pressure.
- With the global semantic gate disabled (the default), citation validation for QA, Summary, and Compare proves only physical citation legality (`source` / `page` or knowledge ID), not that the surrounding claim is semantically supported or that every factual sentence is cited. Enabling `CLAIM_VERIFICATION_ENABLED` adds their model-based claim/evidence gate. Research report generation always enforces fail-closed claim support and atomic-requirement coverage against section-local exact evidence, independently of that rollout switch. Model-based verification still requires calibration against a reviewed domain baseline.
- The rewrite similarity threshold defaults to `0.5` and should be calibrated on real project data.
- Local model downloads may require network access or a pre-populated Hugging Face cache.

## Troubleshooting

- `Rust 扩展 rust_core 未安装` / `缺少: …` — run `make native`, then `make check`.
- Rust edits don't change behavior — you didn't rebuild; the old `.so` is still loaded. Run `make native`.
- `Model Mismatch!` — the index's embedding model differs from `Embedder.MODEL_NAME`; rebuild the index (clear the `doc_id`'s Chroma collection or change `doc_id`).
- Streamlit can't reach the backend — start `make serve` first, and check the **Backend URL** field in the sidebar (defaults to `http://localhost:8000`).
- Hugging Face anonymous-rate warning — set `HF_TOKEN` for higher Hub limits; public models usually download without it.

## License

[MIT](./LICENSE)
