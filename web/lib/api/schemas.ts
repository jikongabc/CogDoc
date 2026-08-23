import { z } from "zod";

const sourceLocationSchema = z.record(z.string(), z.unknown());

const citationSchema = z.object({
  chunk_id: z.string(),
  source_type: z.string(),
  knowledge_id: z.string().default(""),
  source: z.string().default(""),
  source_id: z.string().optional(),
  source_version_id: z.string().optional(),
  media_type: z.string().optional(),
  location: sourceLocationSchema.optional(),
  page: z.number().nullable().optional(),
  page_start: z.number().nullable().optional(),
  page_end: z.number().nullable().optional(),
}).passthrough();

const citationLedgerEntrySchema = citationSchema.extend({
  evidence_id: z.string().regex(/^E[0-9]{3,}$/),
  span_start: z.number().int().nonnegative(),
  span_end: z.number().int().positive(),
  occurrences: z.array(z.object({
    index: z.number().int().nonnegative(),
    answer_start: z.number().int().nonnegative(),
    answer_end: z.number().int().positive(),
  }).refine((occurrence) => occurrence.answer_end > occurrence.answer_start)),
}).refine((entry) => entry.span_end > entry.span_start);

const evidenceSchema = citationSchema.extend({
  parent_chunk_id: z.string().default(""),
  section_title: z.string().default(""),
  section_path: z.string().default(""),
  section_level: z.number().nullable().optional(),
  child_index_in_parent: z.number().nullable().optional(),
  chunk_index: z.number().nullable().optional(),
  rerank_score: z.number().nullable().optional(),
  rewrite_query: z.string().nullable().optional(),
  text_preview: z.string(),
  retrieval: z.record(z.string(), z.unknown()).default({}),
});

export const chatResponseSchema = z.object({
  schema_version: z.literal("v1"),
  request_id: z.string().min(1),
  trace_id: z.string().min(1),
  doc_id: z.string().min(1),
  session_id: z.string().nullable().optional(),
  task_type: z.enum(["qa", "summary", "compare", "unknown"]),
  answer: z.string(),
  citations: z.array(citationSchema),
  citation_ledger: z.array(citationLedgerEntrySchema),
  evidence: z.array(evidenceSchema),
  critique: z.string(),
  is_valid: z.boolean(),
  claim_audit: z.record(z.string(), z.unknown()).nullable().optional(),
  claim_verification: z.record(z.string(), z.unknown()).nullable().optional(),
});

export const streamEventDataSchemas = {
  start: z.record(z.string(), z.unknown()),
  node: z.object({ stage: z.string().optional() }).passthrough(),
  token: z.object({ content: z.string().optional() }).passthrough(),
  final: chatResponseSchema,
  error: z.object({
    schema_version: z.literal("v1").optional(),
    error_code: z.string().optional(),
    message: z.string().default("请求失败"),
    request_id: z.string().nullable().optional(),
    trace_id: z.string().nullable().optional(),
    details: z.record(z.string(), z.unknown()).nullable().optional(),
  }).passthrough(),
} as const;

export type StreamEventName = keyof typeof streamEventDataSchemas;
