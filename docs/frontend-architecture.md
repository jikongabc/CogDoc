# CogDoc 2.0 Frontend Architecture

Status: normative
Scope: complete Next.js web workspace
Last updated: 2026-08-25

## Product objective

CogDoc Web is the primary enterprise product surface for every workflow already
available in the Streamlit client and backend. It must preserve backend state
transitions, permissions, request shapes, audit behavior, and failure semantics.
The target is capability parity, not a demonstration slice.

The Streamlit client is the behavioral reference for:

- account login, registration, invitations, OIDC linking, workspace switching;
- knowledge-base, document, source, connector, credential, ACL, and version work;
- conversational QA, summary, compare, streaming, sessions, citations, evidence,
  feedback, corrections, memory, traces, and retrieval diagnostics;
- derived-knowledge authoring, revision, review, stale repair, batch actions, and
  feedback-loop review;
- Research planning, execution, pause/resume/cancel, provenance, review, publish;
- retrieval-evaluation and claim-verification review desks;
- members, invitations, service accounts, tokens, session policy, SSO/SCIM,
  audit events/exports, index migrations, and operational task inspection.

No route may be represented by a “coming soon” placeholder when its backend
capability already exists.

## Current-state audit

`src/cogdoc/frontend/app.py` is an approximately 8,000-line Streamlit application
and `api_client.py` is an approximately 2,300-line backend adapter. Streamlit
combines navigation, authentication, server state, forms, background SSE work,
and all product domains in one rerun-driven module. The Next.js client now exposes
the complete route map below, including Research, source/version operations,
governance, diagnostics, identity, service accounts, sessions, and audit. Ongoing
parity work is therefore verified workflow by workflow rather than represented by
placeholder routes.

The Next.js migration separates these responsibilities without changing the
workflow itself.

## Information architecture

The Streamlit workspace is the navigation contract. The React application must
remain structurally familiar to an existing CogDoc user: one selected workspace,
one selected knowledge base, its conversations and documents in the left rail,
and the six stable work views in the main area. React routes provide durable
deep links; they do not redefine the user's workflow.

```text
Workspace
├── Context rail
│   ├── Account and workspace switcher
│   ├── Local Ollama mode
│   ├── Knowledge-base select / create
│   ├── Conversation list / create / delete
│   ├── Document upload / index status / delete
│   └── Resource access and destructive controls
├── Main views
│   ├── Conversation             mode / history / streaming / evidence / feedback
│   ├── Research                 plan / run / provenance / review / publish
│   ├── Documents                upload / index status / access / delete
│   ├── Derived knowledge        author / review / conflicts / feedback loop
│   ├── Evidence review          claim verification / retrieval evidence
│   └── Diagnostics              trace / retrieval / migration inspection
└── Incremental product tools
    ├── All knowledge bases
    ├── Data ingestion           connectors / credentials / sync / catalog / versions
    ├── Background tasks
    └── Administration           identity / members / security / audit
```

The selected knowledge base is the primary working context, exactly as in the
Streamlit client. Global list, task, and administration routes remain available
as secondary utilities, but they must never displace, duplicate, or hide the six
work views. Data ingestion is intentionally separate from the RAG document view:
connections operate external systems, while their materialized documents remain
visible in the selected knowledge base.

## Route map

| Route | Primary responsibility |
| --- | --- |
| `/login` | Password, SSO, registration, invitation, or explicit local-mode entry |
| `/home` | Resume the last selected KB/session, or guide first KB creation |
| `/chat` | Compatibility picker that enters a KB conversation |
| `/knowledge` | Find, create, and manage knowledge bases |
| `/knowledge/[kbId]` | Document list, upload, indexing state, access, and delete |
| `/knowledge/[kbId]/sources` | Legacy deep link redirected to Data ingestion |
| `/knowledge/[kbId]/knowledge` | Derived knowledge and feedback loop |
| `/knowledge/[kbId]/access` | KB/document policies and grants |
| `/knowledge/[kbId]/diagnostics` | Trace, retrieval inspection, index operations, and reviewer-only RAG evaluation for the current KB |
| `/knowledge/[kbId]/chat/[sessionId]` | Stateful streaming conversation |
| `/research` | Research queue, plan, lifecycle, provenance, report review and publish |
| `/reviews` | Backward-compatible redirect to the current KB's Diagnostics → RAG 评测 tab |
| `/integrations` | Connections, credentials, sync jobs, external-source catalog and versions |
| `/tasks` | Durable background work across domains |
| `/admin` | Members and invitations |
| `/admin/identity` | OIDC identities/policy and SCIM readiness |
| `/admin/service-accounts` | Machine identities, token policy, token lifecycle |
| `/admin/security` | Password, sessions, and workspace session policy |
| `/admin/audit` | Audit event inspection and export jobs |
| `/admin/workspace` | Workspace identity and destructive settings |

## Runtime boundaries

- Next.js route layouts and static metadata are server components.
- Authenticated screens are client components because the existing API returns a
  bearer token rather than an HttpOnly browser session.
- TanStack Query owns remote data and bounded polling; Zustand owns only session,
  selected workspace, navigation preferences, and ephemeral cross-route context.
- URL segments and search parameters own selected durable resources and filters.
- React Hook Form plus Zod owns every mutation form.
- The same-origin `/api/cogdoc/*` rewrite is the only browser-to-API path.

Workspace switching is a security boundary: cancel active requests, clear query
caches, replace the bearer session returned by the backend, and navigate to Home
before rendering data for the new workspace.

## API client architecture

The client is divided by domain but shares one transport:

```text
apiFetch
├── auth / workspaces / sessions
├── knowledge bases / documents / access
├── connections / credentials / sources
├── chat / traces / feedback / memory
├── derived knowledge / review queues
├── research / provenance / publication
├── evaluations / claim verification / migrations
└── service accounts / identity / audit
```

The transport attaches `Authorization` and `X-CogDoc-Workspace`, supports JSON,
multipart, text/blob downloads, 204 responses, abort signals, and stable
`ApiError` metadata. Protected 401 responses clear the local session. UI
permissions improve affordances only; the backend remains authoritative.

Unknown optional response fields are retained. Fields used for security-sensitive
rendering, navigation, streaming, and evidence binding are runtime validated.

## Streaming and long-running work

Chat follows `idle → connecting → streaming → final|failed|cancelled`. A final SSE
event replaces the partial projection with the authoritative response. EOF without
`final` is an interruption. Partial cancelled text remains visibly incomplete.

Index, connector sync, Research, export, and migration jobs use bounded polling
only while non-terminal and visible. The UI shows real server states rather than
invented percentage progress. Mutations never retry automatically.

## Authentication behavior

`/login` is always a deliberate entry surface:

- account auth enabled: password login plus conditional registration, invitation,
  and OIDC;
- account auth disabled: explain local deployment mode and require an explicit
  “Enter local workspace” action instead of silently bypassing the page;
- account session: expose password change, current sessions, revoke, logout-all,
  OIDC link/unlink, and workspace invitation acceptance in Admin/Security.

The login surface preserves the Streamlit entry model: `登录`, `注册`, and
`接受邀请` are first-class tabs. Registration stays visible but disabled with a
clear deployment-policy explanation when self-registration is off. Invitation
tokens may be prefilled from a safe one-time URL parameter and are removed from
the address bar immediately. Successful login, registration, invitation, OIDC
exchange, and workspace switching all converge on the same validated session
application path.

The browser token remains in session storage for refresh continuity. It is never
placed in URLs, logs, server-rendered props, or analytics.

## Component domains

- `layout`: AppShell, Sidebar, Header, WorkspaceSwitcher, PageHeader,
  ContextNavigation, Inspector.
- `data-display`: DataGrid, Table, StatusBadge, EmptyState, QueryState.
- `ai`: ChatWindow, Message, Composer, Citation, EvidencePanel, SourcePreview,
  VerificationState.
- `knowledge`: KnowledgeBaseList, DocumentList, UploadZone, RoleSelector,
  SourceCatalog, ConnectionEditor, KnowledgeReviewTable.
- `research`: ResearchQueue, PlanEditor, ResearchLifecycle, ReportReview,
  ProvenanceInspector.
- `admin`: MemberManagement, InviteTable, IdentityPolicy, ServiceAccountTable,
  SessionTable, AuditTable.

Pages compose shared primitives and may colocate a workflow-specific editor until
it has a second consumer. They do not implement independent color, spacing,
dialog, table, loading, or error systems.

## Quality and accessibility gates

Every delivered state must pass ESLint, strict TypeScript, production build, and
Playwright. E2E covers authentication modes, workspace isolation, resource CRUD,
upload polling, source sync, streaming/evidence, feedback, Research lifecycle,
review decisions, permission affordances, and critical admin flows with API
fixtures. Critical pages are reviewed at desktop and mobile widths, with keyboard
navigation and reduced motion.

## Migration rule

Streamlit stays available as a fallback until this route map reaches verified
parity. Its information architecture, labels, view order, state transitions, and
user outcomes are normative. Implementation is still decomposed into shared React
domains rather than mechanically translating 8,000 lines, but a redesign may not
move or remove an existing capability merely because another SaaS pattern looks
cleaner. Every deviation requires a demonstrable accessibility, security, or
responsive-layout reason while preserving the original action in context.
