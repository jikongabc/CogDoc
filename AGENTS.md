# CogDoc repository instructions

## Scope and compatibility

- Preserve existing product behavior, API contracts, data formats, ACL semantics, and user workflows unless the task explicitly changes them.
- Keep the Next.js workspace, CLI, API, and legacy Streamlit client compatible. Use `docs/cli-web-parity.md` as the cross-client contract.
- Do not replace an existing implementation with a parallel rewrite. Extend or refactor the current path and remove superseded code only after its callers are migrated.
- Treat an existing dirty worktree as user-owned. Do not discard, overwrite, commit, or push unrelated changes.

## Architecture

- Python application code lives under `src/cogdoc/`; API routes should delegate durable business behavior to service/store layers rather than duplicate it.
- Tenant, workspace, knowledge-base, document, and role checks must remain fail-closed. Retrieval ACLs apply before evidence reaches prompts, traces, exports, or background jobs.
- Keep writes crash-safe where state or publication correctness depends on them: use the repository's atomic-write, lock, lease, epoch, and fencing helpers instead of ad hoc file or database writes.
- Frontend changes must follow `docs/frontend-architecture.md`, `docs/frontend-design-system.md`, and `docs/ui-guidelines.md`.

## Validation

Run checks proportional to the change, then use the broader gates before delivery:

```bash
make lint
make typecheck-security
make test
make web-check
make web-e2e
```

- Rust changes under `rust_core/src/` require `make native` before tests so Python does not load a stale extension.
- Add regression coverage for bug fixes. Prefer focused tests during iteration and the full relevant gate before reporting completion.
- Do not weaken, skip, or delete an existing test merely to make a change pass.

## Repository hygiene

- Never commit `.env`, credentials, account/session tokens, private corpora, local indexes, generated evaluation reports, runtime databases, logs, or machine-specific paths.
- Keep local source documents in `your_documents/` or another ignored location. Only reviewed anonymous examples and curated baseline/calibration summaries belong in Git.
- Preserve the major README structure. Update descriptions and screenshots in place when product behavior changes; do not replace it with a new marketing-style document.
- Keep documentation concise and operational. Avoid temporary plans, status notes, duplicated instructions, and screenshots that are no longer referenced.
