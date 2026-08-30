# CogDoc 2.0 Frontend Design System

Status: normative
Scope: every CogDoc 2.0 web surface
Last updated: 2026-08-30

This document defines the visual primitives for CogDoc 2.0. Product pages may
compose these primitives, but must not introduce local color systems, spacing
scales, radius scales, or typography rules.

## Product character

CogDoc is an enterprise evidence workspace, not a generic administration
dashboard and not an AI toy. Its visual language should feel like a precise
research instrument: quiet chrome, compact controls, strong document hierarchy,
and evidence that is visibly traceable.

The single signature element is the **provenance rail**: citations, source
versions, Research provenance, review evidence, and audit details all open in the
same disciplined right-side inspector. In chat, numbered citations share a stable
identity with exact evidence. In operational surfaces, a selected row shares that
rail with its durable timeline and source metadata. This is functional identity,
not decoration.

## Visual design plan

- Palette: Canvas `#F7F7F7`, Paper `#FFFFFF`, Ink `#171717`, Stone `#676767`,
  Evidence green `#176B5B`, and semantic green/amber/red reserved for status.
- Type: Geist Sans for product and long-form work, the system CJK sans fallback
  for Chinese readability, and Geist Mono only for hashes, IDs, timestamps, and
  diagnostic values.
- Layout: a quiet 272px context rail, 48px work-view header, dense central work
  surface, and a reusable 400px provenance inspector.
- Signature: the provenance rail binds claims and operations to inspectable
  evidence without making users leave their work.

This direction deliberately avoids the generic dashboard pattern of metric-card
mosaics. Product identity comes from traceable information structure, compact
ledger rows, and document-like reading surfaces rather than decoration.

## Design principles

1. **Evidence has hierarchy.** Answers are readable first; provenance is one
   click away; diagnostics never compete with the answer.
2. **Density is deliberate.** Prefer rows, dividers, inline metadata, and split
   panes over repeated cards.
3. **State is explicit.** Loading, syncing, verified, stale, failed, private, and
   restricted states always use text plus an icon or shape. Color alone is not a
   status system.
4. **Enterprise calm.** No gradients, glass effects, oversized radii, decorative
   charts, or ambient animation.
5. **One action, one label.** Controls use stable verbs and sentence case.

## Color tokens

The palette is neutral and nearly monochrome. A low-saturation mineral green is
reserved for evidence, primary actions, and focus; it never becomes a broad
decorative fill.

| Token | Light | Dark (reserved) | Use |
| --- | --- | --- | --- |
| `--background` | `#F7F7F7` | `#111318` | Application canvas |
| `--surface` | `#FFFFFF` | `#181B21` | Primary working surface |
| `--surface-subtle` | `#F3F3F3` | `#20242C` | Hover, selected rows, secondary panes |
| `--surface-raised` | `#FFFFFF` | `#242832` | Popovers and dialogs |
| `--foreground` | `#171717` | `#F4F6FA` | Primary text |
| `--muted-foreground` | `#676767` | `#A9B0BE` | Secondary text |
| `--border` | `#E6E6E6` | `#303641` | Default dividers and inputs |
| `--border-strong` | `#D1D1D1` | `#454D5C` | Emphasized boundaries |
| `--primary` | `#176B5B` | `#6FC7B5` | Evidence, primary action, selected navigation, focus |
| `--primary-hover` | `#11594C` | `#84D2C2` | Primary action hover |
| `--primary-subtle` | `#E7F1EE` | `#17312C` | Selected evidence and citation background |
| `--success` | `#187050` | `#69C697` | Complete, healthy, verified |
| `--success-subtle` | `#E8F3EE` | `#173327` | Success surfaces |
| `--warning` | `#8A5A00` | `#E2B55B` | Stale, partial, waiting |
| `--warning-subtle` | `#FFF6DC` | `#352B16` | Warning surfaces |
| `--error` | `#B3261E` | `#F38B82` | Failed, destructive, invalid |
| `--error-subtle` | `#FCEDEB` | `#3A1D1C` | Error surfaces |

Rules:

- Body text must meet WCAG AA contrast.
- `primary` is the only accent for interactive selection.
- Status colors may appear in icons, left rules, compact badges, and text; never
  as large solid panels.
- Charts, when introduced, derive sequential shades from primary and reserve
  semantic colors for semantic meaning.

## Typography

- Product and body: **Geist Sans**, with `Inter`, `ui-sans-serif`, and system
  fonts as fallbacks.
- Data, IDs, hashes, timestamps where alignment matters: **Geist Mono**, with
  `ui-monospace` fallback.
- Chinese text relies on the system CJK sans fallback after Geist/Inter.

Type scale:

| Role | Size / line | Weight | Notes |
| --- | --- | --- | --- |
| Page title | 24 / 32 | 600 | One per route |
| Section title | 16 / 24 | 600 | Dense content sections |
| Body | 14 / 22 | 400 | Default application text |
| Compact body | 13 / 20 | 400 | Tables, sidebars, metadata |
| Label | 12 / 16 | 500 | Controls and table headers |
| Caption | 11 / 16 | 500 | IDs, timestamps, secondary metadata |

Rules:

- No display-size marketing typography inside the authenticated product.
- Headings use sentence case and restrained tracking.
- Answers use 15px/25px for long-form readability; application chrome remains
  13–14px.

## Spacing

All spacing is based on 4px. Allowed values:

`0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64`

- Sidebar row: 32–36px high.
- Header: 48px high.
- Compact table row: 40px; comfortable row: 48px.
- Main content gutter: 24px desktop, 16px tablet/mobile.
- Reading column: 720–800px; data workspace may use the full available width.
- Context rail: 272px expanded and 56px collapsed. It may scroll independently
  because it contains the original workspace, KB, conversation, and document
  controls rather than only global navigation.
- Provenance rail: 400px desktop; full-height sheet below 1100px.

Do not introduce one-off spacing values unless required for a one-pixel optical
alignment.

## Radius and shadow

Radius tokens:

- `--radius-xs: 4px` — compact controls.
- `--radius-sm: 6px` — buttons and table containers.
- `--radius-md: 10px` — inputs, popovers, dialogs, and upload zones.
- No pill shapes except status chips, avatars, and segmented selections.

Shadow tokens:

- `--shadow-float: 0 16px 40px rgba(23, 23, 23, 0.12)` — dialogs and popovers.
- `--shadow-edge: 0 1px 2px rgba(23, 23, 23, 0.05)` — rare raised surfaces.

Primary page structure is created with borders and background changes, not
stacks of shadows.

## Iconography

- Use Lucide icons at 16px in controls and 18px in primary navigation.
- Default stroke width is 1.75.
- Icons never replace a non-obvious text label.
- Avoid emoji in product chrome.

## Motion

Framer Motion is allowed only for:

- evidence panel enter/exit and citation-to-evidence focus;
- compact row insertion/removal feedback;
- reduced, local layout transitions under 180ms.

No page entrance choreography, parallax, ambient loops, or animated gradients.
Respect `prefers-reduced-motion` by removing non-essential transforms.

## Core component anatomy

### Buttons

- Variants: `primary`, `secondary`, `ghost`, `destructive`.
- Heights: 36px default, 40px form, 32px compact.
- One primary action per local action group.
- Loading preserves width and label, replaces the action icon with the shared
  `Spinner`, disables repeat activation, and exposes `aria-busy`.

### Loading and progress

- Route and full-page boundaries use the shared `LoadingState` with a restrained
  `Spinner` and a concrete status label.
- Local reads use the same spinner at compact size; they do not replace the
  surrounding application shell.
- Upload and indexing use `Progress`. Use determinate progress only when the
  backend reports an authoritative percentage. Otherwise use the indeterminate
  bar with the durable stage label (`uploading`, `queued`, `indexing`).
- Motion stops under `prefers-reduced-motion`; status text remains visible.

### Inputs

- Labels remain visible above fields; placeholders show examples only.
- Inline validation appears below the field without shifting unrelated content.
- Focus ring: 2px primary with 2px canvas offset.

### Tables and grids

- Prefer a single bordered region with horizontal dividers.
- Header remains visible for long tables.
- Metadata is aligned and compact; actions appear on row focus/hover but remain
  keyboard reachable.
- Empty state occupies the table body, not a new card.

### Status badges

- Compact 20px height, 11px/16px type, 3px radius.
- Always include human-readable text.
- Use neutral for ordinary lifecycle states; semantic color only when action or
  risk differs.

### Evidence surfaces

- Citation markers use stable labels such as `[1]`, not source filenames inside
  prose.
- Evidence inspector uses a strong top hierarchy: source, exact location,
  verification state, excerpt, retrieval metadata.
- The selected evidence uses `primary-subtle` and a 2px primary left rule.

### Context rail

- Preserve the established CogDoc grouping order: identity/workspace, knowledge base,
  conversations, then documents and access.
- Use compact section headers and dividers instead of nesting each group in a
  card.
- The selected knowledge base and session are always visible. Creating a KB,
  starting a conversation, uploading a document, and deleting a document remain
  available without navigating to a dashboard.
- Data ingestion, advanced account, task, and administration routes sit in a quiet utility footer;
  they do not replace the working context.

## Accessibility floor

- Every interactive element is keyboard reachable with a visible focus state.
- Dialogs and popovers use Radix focus management.
- Minimum target size is 32px desktop and 40px touch layouts.
- Status is announced through text and appropriate live regions.
- Streaming updates use a polite live region; token-by-token changes do not steal
  focus.
- All motion has a reduced-motion path.
