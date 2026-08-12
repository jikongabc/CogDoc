# CogDoc Backup and Restore

This document covers how to back up local CogDoc runtime state, restore it, and decide when an index rebuild is required.

## Backup and Restore

State worth backing up:

- `data/kb/`: knowledge-base registry, source PDFs, generation state, and ingest journal.
- `data/chroma_db/`: vector collections.
- `data/bm25_db/`: BM25 registry and native index bytes.
- `data/manifests/`: manifests and index contract snapshots.
- `data/state.db`: sessions, index jobs, and—when account auth is enabled—users, salted password hashes, workspaces, memberships, login-session/invitation digests, and resource ACLs.
- `data/feedback/`: feedback and bad cases.
- `logs/traces/`: request traces, if you need debugging or audit history.

Restore order:

1. Stop the API and frontend processes.
2. Restore `data/` and any retained `logs/traces/`.
3. Run `make check` to verify the native extension symbols.
4. Run `make smoke-api` to verify the API skeleton.
5. Start the service and check `/readyz` and `/v1/auth/config`; if authentication is enabled, log in before checking `/v1/knowledge-bases` and the target KB's sources/chunks.

A backup is not proven until you have tested a restore. After index-format or chunk-identity changes, run a small restore drill.

Create a local backup:

```bash
make backup
```

By default this archives `data/` and `logs/traces/` into `backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz`. It does **not** include `.env`. The archive's versioned `backup_manifest.json` records every file's relative path, byte size, SHA-256, and backup creation time, plus non-secret source-root configuration metadata. Backup output remains human-readable for compatibility; pass `--json` when automation needs one JSON object.

`v2` archives receive full per-file integrity verification. The restore tool also accepts legacy `v1` archives after validating safe paths, member types, declared roots, aggregate sizes, and hashes that exist for top-level files. Because `v1` has no per-file hashes inside directory roots, its result is explicitly reported as `verification_level: "degraded"` with a warning; it must not be treated as cryptographic proof of all restored content.

To include `.env`:

```bash
python scripts/backup_state.py --include-env
```

`.env` may contain API keys. Store it only in a controlled location and do not commit or share it. Prefer restoring secrets independently from a secret manager.

Verify an archive without changing runtime state:

```bash
python scripts/restore_state.py backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz --verify-only
```

Restore into an empty drill directory, then inspect the restored `data/` and trace roots:

```bash
mkdir -p /srv/cogdoc-restore-drill
python scripts/restore_state.py backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz \
  --target /srv/cogdoc-restore-drill
```

For an in-place recovery, stop every writer first and use `--target . --force`. A non-empty target is rejected without `--force`. Forced recovery replaces only top-level paths present in the archive; unrelated project files remain in place. The restore validates member types and paths, extracts into a sibling temporary directory, verifies the complete manifest, and only then promotes state with atomic moves and rollback on promotion failure.

### Docker backup and offline restore

The image runs as the non-root `cogdoc` user and sets `COGDOC_BACKUP_DIR=/app/data/backups`, so the default backup destination is writable and persists with the data volume. That output subtree is deliberately excluded from the backup payload; repeated backups do not recursively embed older archives. For a quiesced backup of the named volume used below, stop the API first, then run the same image as a one-shot helper:

```bash
docker stop cogdoc-api
docker run --rm \
  --mount type=volume,src=cogdoc-data,dst=/app/data \
  cogdoc:0.1.0 \
  python /app/scripts/backup_state.py --json
docker run --rm \
  --mount type=volume,src=cogdoc-data,dst=/app/data,readonly \
  cogdoc:0.1.0 \
  python /app/scripts/restore_state.py \
    /app/data/backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz --verify-only
```

Never restore over the live mounted volume. Create a fresh destination volume, verify and restore into temporary container storage as root, then copy the verified `data/` payload and return ownership to the runtime user. The destination-empty check below is intentional; use a new volume name for every drill or recovery:

```bash
docker volume create cogdoc-data-restored
docker run --rm --user 0 \
  --mount type=volume,src=cogdoc-data,dst=/source,readonly \
  --mount type=volume,src=cogdoc-data-restored,dst=/restored \
  --entrypoint /bin/bash cogdoc:0.1.0 -lc '
    set -euo pipefail
    test -z "$(find /restored -mindepth 1 -print -quit)"
    python /app/scripts/restore_state.py \
      /source/backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz \
      --target /tmp/cogdoc-restore
    cp -a /tmp/cogdoc-restore/data/. /restored/
    chown -R cogdoc:cogdoc /restored
  '
```

Start a temporary API against `cogdoc-data-restored`, wait for the image's `/readyz` health check, and verify account login, KB/source counts, and representative authorized/denied retrieval before switching traffic. Keep the old volume unchanged until the rollback window closes. Bind-mount deployments should use the same new-destination pattern on one filesystem; do not run `restore_state.py --target /app --force` inside the normal non-root application container.

Run a restore drill at least once per release and after every index-contract change. Record archive size, verification duration, restore duration, `/readyz` result, KB/source counts, and a representative retrieval result. The local archive is a crash-consistent file copy, not a coordinated database snapshot: stop writers before backup when a zero-loss restore point matters. Therefore the achievable RPO is the time since the last completed, quiesced backup; changes after it are not recoverable. RTO includes archive transfer, full SHA-256 verification, extraction, native/index compatibility checks, and any required index rebuild, so large Chroma/BM25 stores can dominate recovery time. Set operational RPO/RTO targets only after measuring them with production-sized restore drills.

## Account Authentication and ACL Rollout

`COGDOC_ACCOUNT_AUTH_ENABLED=false` deliberately preserves the old local/API-key behavior. A production individual or team installation should enable it explicitly. The module-level service then creates the authentication and resource-ACL tables in `COGDOC_DATA_DIR/state.db`; configured static API principals continue to work for service automation. Human login and invitation tokens are returned only to the caller and only their SHA-256 digests are stored. Passwords are salted, versioned scrypt hashes, with a 12-character minimum, bounded failure lockout, and configurable login-session/invitation TTLs.

For a new installation:

1. Put the service behind TLS and stop all writers before taking a verified backup.
2. Set `COGDOC_ACCOUNT_AUTH_ENABLED=true` and initially leave `COGDOC_SELF_REGISTRATION_ENABLED=true`.
3. Start the API, confirm `GET /v1/auth/config`, register the first owner, and capture the returned Bearer token in a secret store. Do not put it in a URL, command history, application log, or trace.
4. Create the required workspaces, invite members with least-privilege roles, deliver each one-time invite token through a separate protected channel, and revoke unused invitations.
5. For an enterprise, set `COGDOC_SELF_REGISTRATION_ENABLED=false` and restart after the bootstrap owner is proven usable. Keep at least one tested owner recovery path; setting registration false before any owner exists locks out account bootstrap.
6. Exercise login, workspace switching, member removal, session revocation, KB/document ACLs, a denied query, and a permitted Research run before exposing traffic.

For an existing static-principal installation, enable accounts as a staged migration rather than assuming principal records become users. There is no automatic conversion from `COGDOC_API_PRINCIPALS` to password accounts, and newly registered account workspaces receive new IDs. Before changing the flag, export an inventory of every tenant/KB ID and retain the old service principals during the migration; once ACL enforcement starts, KBs without a policy disappear from ordinary lists by design. A tenant-matched owner/admin service key must initialize each known pre-existing KB with `PATCH /v1/knowledge-bases/{kb}/access`; missing ACL rows deny access even to administrators. Use `GET /v1/knowledge-bases/{kb}/documents` to obtain stable `document_id` values before adding per-document policies or grants. To move a KB into a newly generated account workspace, create it in that workspace and re-ingest its original PDFs through the supported API/CLI path; do not rewrite registry or ACL tenant IDs directly in SQLite. Remove legacy keys only after account workspaces, policies, memberships, quotas, traces, and Research artifacts have been verified.

This release adds `document_id = source-name-v1` metadata and bumps the chunk-identity contract to `source_sha256_name_page_span_local_v6_document_acl_parent_child_section_index_cs600_ov60_min30_ctx160`. Treat that as rebuild-required: rebuild every affected PDF vector/BM25 generation through normal ingestion before considering the ACL rollout complete. A SUBSET authorization is pushed into vector and BM25 selection before top-k and is checked again after fusion. Background Research persists the creator and frozen allowlist, revalidates current membership/ACLs around retrieval, and fails closed when access is revoked or the backend cannot enforce a subset.

The local identity implementation is intended for one writable CogDoc service instance sharing one protected data directory. Do not place independent `state.db` files behind a load balancer and expect sessions, invites, memberships, or ACL epochs to converge. Restrict filesystem permissions and encrypt backups because `state.db` contains password hashes and live-token digests; possession of a live raw Bearer or invite token is still sufficient for its allowed action. This release does not provide email delivery, email verification, password-reset mail, MFA, or an external IdP/SSO bridge. Enterprises requiring those controls should keep the service behind an appropriate identity-aware gateway/private network and perform a dedicated integration rather than weakening local session checks.

Do not set the singular `COGDOC_API_KEY` in a shared Streamlit deployment. It is an outbound client credential: when present, every browser without its own human session deliberately skips the login screen and operates as that service principal. Keep it empty for multi-user frontends, and configure API-side service identities only with `COGDOC_API_KEYS` or `COGDOC_API_PRINCIPALS`. Use the singular variable only for a trusted single-user console or a dedicated automation frontend.

Preserve `X-CogDoc-Workspace` through any reverse proxy. It is a non-secret selector, not independent authority: the API binds it to the authenticated session's live membership on every request. Modern clients send it to keep concurrent tabs pinned to their intended workspaces; older clients that omit it retain active-workspace compatibility. Reject or overwrite attempts by an upstream gateway to inject a different value, and do not route/cache responses solely by this header without also partitioning on authenticated identity.

Rollback is operational, not a schema deletion: stop writers, restore the matching verified `state.db`/KB/index backup, restore the prior configuration, and restart. Merely setting account auth back to false causes account/ACL tables to be ignored but does not remap account workspace IDs to legacy tenants, and it must not be used as a data migration shortcut. Keep the pre-rollout archive and static service credentials for the entire rollback window.

## Unified SQLite State Migration

The default remains `COGDOC_STATE_BACKEND=jsonl`. Do not change the backend before the migration has completed and passed verification. Stop the API, workers, and every other process that can write sessions, jobs, research plans, feedback, analysis, derived knowledge, or retrieval feedback, then run the migration against the same instance in this order:

```bash
python scripts/migrate_state.py
python scripts/migrate_state.py --apply
python scripts/migrate_state.py --verify-only
```

The first command is a dry run and must not modify state. `--apply` acquires the same-instance migration lock, copies the existing JSONL state while preserving sessions and jobs, builds a temporary unified database, performs a full canonical-record comparison, and atomically replaces `state.db` only after every store matches. `--verify-only` independently compares the committed SQLite state with the canonical source records. Only after all three commands succeed should you set:

```bash
COGDOC_STATE_BACKEND=sqlite
```

Start the service and check `/readyz`, session history, outstanding/completed index jobs, feedback counts, derived knowledge, and a representative retrieval-feedback query. Keep `state.db.pre-unified-*.bak` and the original JSONL files for the entire rollback window; they are recovery artifacts, not files to clean up immediately.

Research evidence runs are section-granular and restart-safe. If the service exits while a research job is `running`, startup resets an in-flight section to `pending` and reconciles the job to `paused`; an operator or user must explicitly resume it. Report generation re-runs each atomic requirement through the closed-set Evidence Unit verifier and only `supported` grounding IDs may reach section generation. Generated claims are then audited against that section-local exact evidence, and an independent obligation audit requires every atomic requirement to be answered by supported cited claims. Claim and coverage failures share at most one bounded repair; all citation, claim, and coverage gates run again before release. No evidence, contradictions, verifier failures, omitted requirements, semantic-audit failures, and generation failures remain explicit report gaps. An interrupted `generating` job returns to `evidence_ready` for an explicit retry while preserving a selective-regeneration scope. The store contains bounded evidence previews, coordinates, the public citation ledger, bounded claim/coverage summaries, and the rendered Markdown report—not full source chunks or model claim text.

Every evidence/report attempt has a durable attempt ID, a renewable lease, a phase deadline, and atomic budgets for retrieval queries, candidate documents, model calls, and aggregate model-input characters. Resume always rotates the lease, so a draining or delayed worker cannot reserve more resources or commit stale output. The admitted queued/running population is capped by `COGDOC_RESEARCH_MAX_PENDING`; excess start/generate requests return `503` with `Retry-After`. Pause and cancel atomically invalidate evidence and report leases, signal active workers, and cancel futures that have not started. Deadline or budget exhaustion is persisted and fails closed.

Automatic planning source/model work runs on its own bounded daemon executor (`COGDOC_RESEARCH_PLANNING_WORKERS` / `COGDOC_RESEARCH_PLANNING_MAX_PENDING`) rather than the shared API offload pool; the short initial/final store operations still use the shared pool. Its absolute deadline covers queue wait, source reading, and model work. Lifespan shutdown signals every registered planning control, cancels queued work, and defers runtime closure and process-lock release while an opaque in-process source reader is still draining. `make serve` also sets Uvicorn's graceful-request shutdown ceiling from `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` (default `15`); deployments using another launcher must configure an equivalent finite ceiling, otherwise Uvicorn can wait for active HTTP handlers before it enters lifespan shutdown. A raw socket disconnect is not itself a portable ASGI cancellation signal, so the dedicated capacity and absolute planning deadline remain the outer bound in that case.

Standard factory-built `ChatOpenAI` calls in automatic planning and evidence/report generation are reconstructed in fresh spawn children with transport retries disabled. The supervisor contracts each call timeout against the remaining planning or durable phase deadline, polls the process-local stop signal and monotonic deadline while the child is live, performs authoritative durable checkpoints before admission and after reap, and always joins and reaps the child; timeout, pause, cancel, or shutdown sends terminate and escalates to kill after `COGDOC_RESEARCH_PROVIDER_KILL_GRACE_SECONDS`. Size `COGDOC_RESEARCH_PROVIDER_WORKERS` and `COGDOC_RESEARCH_PROVIDER_MAX_PENDING` for the provider capacity available to background Research attempts, keep `COGDOC_RESEARCH_PROVIDER_CALL_TIMEOUT_SECONDS` below upstream load-balancer timeouts, and treat `COGDOC_RESEARCH_PROVIDER_IPC_MAX_BYTES` as a fail-closed response-envelope limit. A recognized `ChatOpenAI` client that cannot be converted into a safe child recipe fails closed while `COGDOC_RESEARCH_LLM_PROCESS_ISOLATION_ENABLED=true`; opaque/nonstandard clients retain the bounded daemon compatibility path and can only stop cooperatively at a checkpoint. Graceful shutdown invalidates all active leases before ending the application lifespan, and late compatibility-path results cannot commit.

Timeout accounting begins before spawn and includes provider-slot wait and child lifetime. Python's local spawn bootstrap and the bounded IPC frame decode are trusted admission/serialization boundaries: their elapsed time is charged to the deadline, but the interpreter cannot asynchronously preempt those short synchronous operations themselves. Factory calls are pre-serialized into a bounded plain-byte recipe before spawn to keep this boundary deterministic.

This isolation ends only the local HTTP-client process. An already-contacted remote API or Ollama server may continue computing and charging, so provider-side request IDs, budgets, and billing alerts remain necessary. Retrieval, reranking, embeddings, Hugging Face model loading, Torch kernels, and native/Rust calls still execute in-process; the Research controller checks deadlines around them but cannot forcibly preempt a blocking call. Do not describe this release as arbitrary-provider or full-pipeline sandboxing.

Collection views should poll `GET /v1/research-jobs/summaries` rather than the compatibility full-list endpoint. The summary endpoint uses bounded keyset pages (`limit` plus opaque `cursor`), returns an ETag, honors `If-None-Match` with `304`, and excludes sections, evidence, reports, and history bodies. Fetch one job detail and its report only after explicit selection. Monitor `cogdoc_research_lifecycle_total`, `cogdoc_research_background_total`, `cogdoc_research_background_in_progress`, `cogdoc_research_terminations_total`, `cogdoc_research_provider_calls_total`, provider-call duration, section candidate/evidence histograms, and coverage/claim audit counters; their labels are closed low-cardinality enums, while IDs remain log fields rather than metric labels.

Research publication is a separate optimistic-concurrency transition. Explicit `reviewer`, `admin`, or `owner` principals are authorized by RBAC and persist their `subject_id`; legacy deployments may instead use `COGDOC_EVAL_REVIEW_API_KEYS`, which persists a non-secret key fingerprint identity. Each evidence run freezes index generation/build/chunk identity, source SHA-256 values, approved-derived-knowledge revision, retrieval-tuning revision, and the retrieval/verification contract revision. Any drift blocks generation, review, and publication with a stale status; explicit refresh archives the old report, clears every section's evidence/audit output, and starts a full run against a new snapshot. Generated sections require `approved`; blocked sections require explicit `accepted_gap` plus a non-blank rationale; any `changes_requested` decision must include an instruction and permits regeneration only through the same retrieval/verifier pipeline. Regeneration archives up to ten complete report versions and review history is bounded to 100 events. Only rejected or legacy-unaudited sections consume retrieval/verifier/generation work; preserved and regenerated section-local ledgers are re-keyed and rebased into a newly validated global ledger. The v2 artifact SHA-256 binds the exact Markdown, strict citation ledger, trackable provenance, bounded aggregate/per-section claim and requirement-coverage audits, evidence identity/hash commitments, report version, and generation timestamp. A separate publication SHA-256 binds that artifact to the exact review history, per-section decisions, publication time, and reviewer identity. The deterministic ZIP contains `report.md`, `citation-ledger.json`, `provenance.json`, `verification.json`, and a per-file hash manifest. Legacy published Markdown remains available with `X-CogDoc-Integrity: legacy-unverified`, but cannot produce a verification bundle; any malformed or tampered artifact is withheld.

If dry-run, apply, or verification fails, keep the service stopped and do not switch the backend. Capture the command's JSON error, confirm that no stale migration process owns the instance lock, check free disk space and permissions for the data directory, and resolve malformed or duplicate canonical records before rerunning the dry run. Never promote a temporary database manually.

To roll back after a failed SQLite startup or post-migration check:

1. Stop the API and all state writers.
2. Set `COGDOC_STATE_BACKEND=jsonl` (or remove the SQLite override).
3. Preserve the failed `state.db` for diagnosis; do not overwrite the retained JSONL files.
4. If the unified database replaced a pre-existing `state.db`, restore the matching `state.db.pre-unified-*.bak` only for components that still require that legacy database.
5. Restart the service, verify sessions/jobs and feedback state from JSONL, and repeat the migration from dry-run after the cause is fixed.

The migration lock only serializes cooperating migration processes for one instance; it does not make live application writes safe. Stopping all writers is therefore a required operational precondition.

## Index Format and Migration

Treat these changes as index-contract changes:

- `CHUNK_IDENTITY_BASE_VERSION` or chunking parameter changes.
- `INDEX_BUILD_VERSION` changes.
- Parser, tokenizer, embedding model, or BM25 artifact format changes.
- Chroma collection naming or generation layout changes.

Rules:

- Reusable changes: API, frontend, and prompt-only changes usually do not require an index rebuild.
- Rebuild-required changes: chunk identity, parser/tokenizer, embedding model, or BM25 bytes format changes.
- If a migration is needed, state whether a rebuild is required, whether old generations remain compatible, and how to roll back after failure.
