<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## CogDoc Web workspace

- This directory is the primary Next.js product workspace. Preserve API request/response shapes and existing business workflows; backend behavior is not to be reimplemented in the browser.
- Follow `../docs/frontend-architecture.md`, `../docs/frontend-design-system.md`, and `../docs/ui-guidelines.md`. The intended product character is quiet, dense, accessible, and content-first—avoid gradients, glass effects, decorative cards, and unnecessary motion.
- Reuse shared primitives in `components/ui`, layout components, query keys, the typed API client, React Hook Form + Zod forms, TanStack Query server state, and Zustand client state.
- Keep loading, empty, error, disabled, streaming, cancellation, and responsive states functional. Buttons performing asynchronous work need a loading state; uploads need progress when the backend exposes it.
- Do not hide authorization failures or convert them into empty data. Render actionable 401/403/409/503 states while preserving the server error contract.
- Validate changes with `npm run lint`, `npm run typecheck`, and `npm run build`; run the relevant Playwright workflows for user-facing behavior.
