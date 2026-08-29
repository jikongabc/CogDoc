"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { BookmarkPlus, Check, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import type { ChatResponse } from "@/lib/api/types";
import { api } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { usePermission } from "@/features/auth/permissions";
import { controlApi } from "@/lib/api/control-plane";

const feedbackSchema = z.object({
  issue: z.enum(["no_evidence", "wrong_answer", "bad_retrieval", "correction", "other"]),
  feedbackText: z.string().max(2000, "最多 2000 个字符").optional(),
  correctionText: z.string().max(4000, "最多 4000 个字符").optional(),
}).superRefine((values, context) => {
  if (values.issue === "correction" && !values.correctionText?.trim()) {
    context.addIssue({ code: "custom", path: ["correctionText"], message: "请填写建议的正确内容" });
  }
});
type FeedbackValues = z.infer<typeof feedbackSchema>;

export function FeedbackControls({ kbId, sessionId, query, response }: { kbId: string; sessionId: string; query: string; response: ChatResponse }) {
  const [submitted, setSubmitted] = useState<"thumbs_up" | "thumbs_down" | null>(null);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const canWrite = usePermission("write");
  const form = useForm<FeedbackValues>({ resolver: zodResolver(feedbackSchema), defaultValues: { issue: "wrong_answer", feedbackText: "", correctionText: "" } });
  const issue = useWatch({ control: form.control, name: "issue" });

  const basePayload = { trace_id: response.trace_id, kb_id: kbId, session_id: sessionId, query, answer: response.answer, citations: response.citations, evidence: response.evidence };
  const positive = async () => {
    setPending(true);
    try { await api.feedback({ ...basePayload, feedback: "thumbs_up" }); setSubmitted("thumbs_up"); toast.success("反馈已记录"); }
    catch (error) { toast.error(error instanceof Error ? error.message : "反馈提交失败"); }
    finally { setPending(false); }
  };
  const negative = form.handleSubmit(async (values) => {
    setPending(true);
    try {
      await api.feedback({ ...basePayload, feedback: values.issue === "correction" ? "correction" : "thumbs_down", feedback_type: values.issue, feedback_text: values.feedbackText || undefined, correction_text: values.correctionText || undefined });
      setSubmitted("thumbs_down"); setOpen(false); toast.success("反馈已记录");
    } catch (error) { form.setError("root", { message: error instanceof Error ? error.message : "反馈提交失败" }); }
    finally { setPending(false); }
  });

  const saveAnswer = async () => {
    setSaving(true);
    try {
      const cited = response.citation_ledger.length ? response.citation_ledger : response.citations;
      const chunkIds = [...new Set(cited.map((citation) => citation.chunk_id).filter(Boolean))];
      const sources = [...new Set(cited.map((citation) => citation.source).filter(Boolean))];
      const pages = cited.flatMap((citation) => [citation.page_start, citation.page_end, citation.page]).filter((page): page is number => typeof page === "number");
      await controlApi.createKnowledge({
        kb_id: kbId,
        text: response.answer,
        certainty: "medium",
        origin: "saved_answer",
        created_from_trace_id: response.trace_id,
        related_source: sources.length === 1 ? sources[0] : undefined,
        related_chunk_ids: chunkIds,
        related_page_start: pages.length ? Math.min(...pages) : undefined,
        related_page_end: pages.length ? Math.max(...pages) : undefined,
        source_note: chunkIds.length ? `由回答引用的 ${chunkIds.length} 条证据创建` : "由未引用证据的回答创建，需人工补充来源",
      });
      setSaved(true);
      toast.success("回答已保存为待审核派生知识");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="flex items-center gap-1">{submitted ? <span className="mr-1 flex items-center gap-1.5 text-xs text-success"><Check className="size-3.5" />反馈已记录</span> : <><Button variant="ghost" size="icon" onClick={positive} disabled={pending} aria-label="回答有帮助"><ThumbsUp className="size-3.5" /></Button><Button variant="ghost" size="icon" onClick={() => setOpen(true)} disabled={pending} aria-label="回答需要改进"><ThumbsDown className="size-3.5" /></Button></>}{canWrite ? <Button variant="ghost" size="compact" onClick={saveAnswer} loading={saving} disabled={saved}><BookmarkPlus className="size-3.5" />{saved ? "已保存" : "保存为派生知识"}</Button> : null}</div>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>这条回答需要改进</DialogTitle><DialogDescription>选择最主要的问题，帮助审核人员定位回答或检索链路。</DialogDescription></DialogHeader>
          <form onSubmit={negative} className="space-y-4">
            <div className="space-y-1.5"><Label id="feedback-issue-label">问题类型</Label><Select value={issue} onValueChange={(value) => form.setValue("issue", value as FeedbackValues["issue"])}><SelectTrigger className="w-full" aria-labelledby="feedback-issue-label"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="wrong_answer">答案不正确</SelectItem><SelectItem value="no_evidence">缺少证据</SelectItem><SelectItem value="bad_retrieval">检索内容不相关</SelectItem><SelectItem value="correction">我有更正</SelectItem><SelectItem value="other">其他问题</SelectItem></SelectContent></Select></div>
            <div className="space-y-1.5"><Label htmlFor="feedback-detail">补充说明（可选）</Label><Textarea id="feedback-detail" placeholder="指出具体问题，避免包含敏感信息。" {...form.register("feedbackText")} />{form.formState.errors.feedbackText ? <p className="text-xs text-error">{form.formState.errors.feedbackText.message}</p> : null}</div>
            {issue === "correction" || issue === "no_evidence" ? <div className="space-y-1.5"><Label htmlFor="correction">{issue === "correction" ? "正确内容" : "可补充的依据或正确内容（可选）"}</Label><Textarea id="correction" placeholder={issue === "correction" ? "写下建议的正确表述。" : "填写后会创建一条待审核的“无证据”派生知识。"} {...form.register("correctionText")} />{form.formState.errors.correctionText ? <p className="text-xs text-error">{form.formState.errors.correctionText.message}</p> : null}</div> : null}
            {form.formState.errors.root ? <p className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{form.formState.errors.root.message}</p> : null}
            <DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={pending}>记录反馈</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
