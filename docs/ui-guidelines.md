# CogDoc 2.0 UI Guidelines

Status: normative
Scope: copy, composition, interaction, and review
Last updated: 2026-08-24

## Experience thesis

The authenticated product should feel like a dependable evidence workspace on
first open. It borrows the conversational clarity of ChatGPT Enterprise, the
information hierarchy of Notion, the density and directness of Linear, and the
settings discipline of Vercel without copying any product's visual skin.

The defining interaction is a calm split workspace: work remains in the center;
sources and evidence open in a contextual rail. Users retain place and context.

## Application frame

Desktop:

```text
┌──────────────────────┬──────────────────────────────────────────────┐
│ Account / workspace  │ Conversation Research Sources Knowledge ... │
│ Knowledge base       │──────────────────────────────────────────────│
│ + New conversation   │                                              │
│ Conversation history │ Main working surface             Evidence   │
│──────────────────────│                                  inspector  │
│ Upload document      │                                              │
│ Document list        │                                              │
│ Access / utilities   │                                              │
└──────────────────────┴──────────────────────────────────────────────┘
```

- Context rail is 288px expanded and 56px collapsed.
- Work-view header is 44px and preserves the original order: 对话、研究、来源、
  派生知识、证据审核、调试.
- Main content fills available width. Reading content gets an internal maximum
  width; tables and split workspaces do not.
- Evidence rail is 380–440px, resizable later, and overlays only below desktop.

Mobile/tablet:

- Sidebar becomes a Radix sheet.
- Evidence becomes a full-height sheet.
- Tables preserve critical columns and expose remaining metadata in a row detail.
- Chat composer remains visible above safe-area insets.

## Navigation

Primary work-view order is stable and inherited from Streamlit:

1. 对话
2. 研究
3. 来源
4. 派生知识
5. 证据审核
6. 调试

Rules:

- Workspace switcher sits above knowledge context because it changes the data
  boundary for every route.
- Knowledge-base, conversation, and document controls remain in the context rail,
  matching the established CogDoc workflow.
- The active work view uses a subtle background and bottom/inner marker, not
  a filled brand-color block.
- Home, all-knowledge, tasks, and admin are secondary utility destinations. They
  may not replace or reorder the six work views.
- Breadcrumbs identify location; they are not a second navigation menu.
- Preserve deep links for knowledge bases and sessions.

## Page composition

Each route has:

1. a compact header with title, optional breadcrumb, state, and one primary
   action;
2. an optional filter/action bar;
3. one dominant work region;
4. contextual details in a side panel or dialog.

Avoid dashboard mosaics. If several facts describe one resource, present them in
one header or structured row rather than separate metric cards.

## Workflow guidance

### Login

- Use one restrained authentication workspace with the same three entry choices
  as Streamlit: 登录、注册、接受邀请. A quiet product context area may appear on
  wide screens, but must not read as a marketing hero.
- Never use a marketing hero, testimonials, floating shapes, or gradients.
- Show enterprise sign-in only when the auth configuration permits it.
- Keep the registration tab visible when account authentication is enabled. If
  self-registration is disabled, explain that an invitation is required rather
  than silently removing the tab.
- When account auth is disabled, show a clear local-deployment explanation and
  an explicit entry action; never auto-redirect past the login surface.
- Errors state the action needed: “Check your email and password,” not “Something
  went wrong.”

### Workspace selection

- Show name, role, and current marker.
- Changing workspace is an explicit data-boundary operation. Clear old data and
  show a short loading state.
- Never display stale previous-workspace data behind the new selection.

### Knowledge

- The list is table/row-led. A knowledge base is not a decorative card gallery.
- `KnowledgeBaseCard` is a compact selectable resource tile reserved for empty,
  recent, or picker contexts.
- Creation asks only for current backend fields: ID and access policy.
- Empty state: “Create a knowledge base to add sources and ask questions.” with
  one `Create knowledge base` action.

### Upload

- Upload zone supports drag/drop and keyboard file selection.
- Display accepted formats and backend limits without implying client-side
  guarantees.
- One row represents one upload lifecycle: queued, indexing, ready, or failed.
- During polling, communicate the latest durable server state. Do not use fake
  percentage progress when the API exposes no percentage.
- Success reports document and chunk counts. Failure keeps the filename and gives
  a retry path.

### Chat

- The conversation is a reading surface, not a stack of speech-bubble cards.
- User turns have restrained surface contrast; assistant turns use the page
  background and clear role metadata.
- Composer supports multiline input, Enter to send, Shift+Enter for newline, and
  an explicit mode selector.
- During streaming, show a compact current stage and a Stop action. Do not animate
  every token.
- Preserve partial text on an interrupted stream and mark it incomplete.
- Follow new output while the reader is near the bottom. If they scroll upward,
  preserve their reading position and offer a compact `查看新回答` action.
- Never render raw model HTML.

### Citation and evidence

- Inline markers map to the authoritative citation ledger whenever present.
- Hover/focus gives a short source label; activate opens the evidence rail.
- The evidence rail shows source and location before excerpt text.
- Exact evidence is distinguished from neighboring context.
- Missing evidence does not silently disappear: label the citation as unavailable
  and preserve the answer's validity state.
- Verification summaries use plain language such as `Verified`, `Needs review`,
  or `Blocked`, never model-internal jargon as the primary label.

### Feedback

- Thumbs up is one-step and confirms `Feedback recorded`.
- Thumbs down opens a compact structured follow-up for wrong answer, missing
  evidence, poor retrieval, correction, or other.
- Do not imply that feedback immediately retrains a model.
- A submitted state is immutable in the current view unless the backend supports
  revision.

## Required reusable components

### Layout

- `AppShell`: responsive frame and skip link.
- `Sidebar`: primary navigation and account footer.
- `Header`: route identity and contextual actions.
- `WorkspaceSwitcher`: workspace boundary selection with role metadata.

### Data display

- `Table`: semantic table primitives.
- `DataGrid`: sorting/selection wrapper for structured resources.
- `Timeline`: ordered lifecycle events with real sequence meaning.
- `ActivityFeed`: reverse-chronological actor/action records.

### AI

- `ChatWindow`: message list, stream state, empty state, and composer slot.
- `Message`: safe answer rendering and role metadata.
- `Citation`: accessible ledger-linked trigger.
- `EvidencePanel`: focus-managed contextual evidence inspector.
- `SourcePreview`: source identity, location, excerpt, and retrieval metadata.

### Knowledge

- `KnowledgeBaseCard`: compact resource selector, not the default list layout.
- `DocumentList`: dense document table and lifecycle state.
- `UploadZone`: accessible file drop/select and job feedback.

### Admin

- `MemberTable`: identity, role, state, and row actions.
- `PermissionEditor`: explicit policy and grant editing with effective-access copy.

## Copy system

- Use user-recognizable nouns: `Knowledge`, `Documents`, `Evidence`, `Members`.
- Prefer direct verbs: `Create`, `Upload`, `Ask`, `Stop`, `Save`, `Publish`.
- Do not expose implementation terms such as graph node, vector database, RRF, or
  mutation journal in primary UI. They may appear in diagnostics.
- Keep action labels stable from button to confirmation.
- Empty states explain the next useful action.
- Errors include what failed and what the user can do next.

## Loading, empty, and error states

- Use skeletons only where the final geometry is known.
- Use a compact spinner for actions with uncertain geometry.
- Never replace the whole application shell for a local refresh.
- Empty state belongs inside the region that is empty.
- Retry only safe reads automatically; mutations require explicit user action.
- Preserve server error codes for support details, but lead with plain-language
  recovery guidance.
- A backend-supported domain may not ship as a “coming soon” or deferred
  placeholder. If a list is empty, the state must still expose its real creation,
  connection, or recovery action.

## Product surface composition

- Home is a work queue, not an analytics dashboard. Show pending reviews,
  running/failed work, recent knowledge bases, and direct next actions in rows.
- Knowledge-base pages share one local navigation for documents, sources,
  derived knowledge, access, and diagnostics. Keep the active KB visible.
- Research uses a master/detail workbench: queue left, plan/report center,
  lifecycle and provenance in the inspector.
- Reviews use queue/detail composition with evidence adjacent to the decision.
- Tasks aggregate durable jobs by lifecycle and link back to the owning resource.
- Admin uses a stable section sidebar and dense settings forms; never put all
  enterprise controls into one endless page.

## Interaction rules

- Destructive actions require a confirmation dialog naming the resource.
- Menus and dialogs use Radix primitives and restore focus on close.
- Row click and row action must not conflict; actions stop propagation.
- Optimistic updates are allowed only for reversible, low-risk UI state. Resource
  creation, workspace switching, upload, feedback, and permissions wait for
  server confirmation.
- Toasts confirm completed actions; persistent problems remain inline.

## UI review checklist

Every page review asks:

- Is there one obvious work surface rather than a grid of cards?
- Does the page remain useful at enterprise information density?
- Can a keyboard user complete the primary flow?
- Are workspace and permission boundaries visible without being noisy?
- Does every status use text in addition to color?
- Are citations visibly connected to exact evidence?
- Are loading and failure states honest about backend state?
- Is any animation decorative rather than informative?
- Could this be mistaken for a generic admin template or AI chat demo? If yes,
  remove decoration and strengthen the workflow-specific hierarchy.

## Prohibited patterns

- Gradients, glassmorphism, glowing borders, oversized hero copy.
- Repeated rounded cards for every content group.
- Emoji as navigation or status iconography.
- Rainbow category colors.
- Hidden labels that rely only on placeholder text.
- Infinite spinners without status or recovery.
- Fake analytics, fake progress, or invented backend capabilities.
- Page-by-page replication of Streamlit's layout.
