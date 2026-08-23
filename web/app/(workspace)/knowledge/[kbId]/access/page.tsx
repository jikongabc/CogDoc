"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LockKeyhole, Plus, Shield, Trash2, Users } from "lucide-react";
import { useParams, useRouter } from "next/navigation";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useDocuments } from "@/features/knowledge/queries";
import { controlApi, isRecord, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { decodeRouteParam } from "@/lib/routing";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { usePermission } from "@/features/auth/permissions";
import { useSessionStore } from "@/stores/session-store";
import { useWorkspaceStore } from "@/stores/workspace-store";

function GrantDialog({ label, onGrant }: { label: string; onGrant: (subject: string, role: string) => Promise<unknown> }) {
  const [open, setOpen] = useState(false);
  const form = useForm({ defaultValues: { subject: "", role: "viewer" } });
  const role = useWatch({ control: form.control, name: "role" });
  const submit = form.handleSubmit(async (values) => {
    try { await onGrant(values.subject.trim(), values.role); toast.success("访问授权已添加"); setOpen(false); form.reset(); } catch (error) { form.setError("root", { message: error instanceof Error ? error.message : "授权失败" }); }
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><Plus className="size-4" />添加授权</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>添加{label}授权</DialogTitle><DialogDescription>主体 ID 与角色将直接发送到现有 ACL 服务。</DialogDescription></DialogHeader><form onSubmit={submit}><div className="space-y-4"><div className="space-y-1.5"><Label htmlFor="subject-id">主体 ID</Label><Input id="subject-id" {...form.register("subject", { required: true })} /></div><div className="space-y-1.5"><Label>角色</Label><Select value={role} onValueChange={(value) => form.setValue("role", value)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["viewer", "reviewer", "editor", "admin"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div>{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary">添加授权</Button></DialogFooter></form></DialogContent></Dialog>;
}

function GrantList({ rows, onRevoke }: { rows: JsonRecord[]; onRevoke: (row: JsonRecord) => void }) {
  return rows.length ? <div className="divide-y divide-border">{rows.map((row) => <div key={textValue(row.subject_id, textValue(row.id))} className="grid grid-cols-[minmax(0,1fr)_120px_auto] items-center gap-3 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.display_name, textValue(row.subject_id))}</p><p className="font-mono text-[10px] text-muted-foreground">{textValue(row.subject_id)}</p></div><Badge>{textValue(row.role)}</Badge><Button variant="ghost" size="icon" className="text-error" onClick={() => onRevoke(row)} aria-label="撤销授权"><Trash2 className="size-4" /></Button></div>)}</div> : <EmptyState icon={Users} compact title="没有直接授权" description="当前访问由策略和继承规则决定。" />;
}

export default function AccessPage() {
  const params = useParams<{ kbId: string }>();
  const kbId = decodeRouteParam(params.kbId);
  const router = useRouter();
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const canDelete = usePermission("delete");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const documents = useDocuments(kbId);
  const [documentId, setDocumentId] = useState("");
  const activeDocumentId = documentId || documents.data?.[0]?.document_id || "";
  const kbPolicy = useQuery({ queryKey: ["access", "kb-policy", kbId], queryFn: () => controlApi.kbAccess(kbId) });
  const kbGrants = useQuery({ queryKey: ["access", "kb-grants", kbId], queryFn: () => controlApi.kbGrants(kbId) });
  const documentPolicy = useQuery({ queryKey: ["access", "document-policy", kbId, activeDocumentId], queryFn: () => controlApi.documentAccess(kbId, activeDocumentId), enabled: Boolean(activeDocumentId) });
  const documentGrants = useQuery({ queryKey: ["access", "document-grants", kbId, activeDocumentId], queryFn: () => controlApi.documentGrants(kbId, activeDocumentId), enabled: Boolean(activeDocumentId) });
  const kbPolicyRow = isRecord(kbPolicy.data) ? kbPolicy.data : {};
  const documentPolicyRow = isRecord(documentPolicy.data) ? documentPolicy.data : {};
  const kbGrantRows = records(kbGrants.data, ["items", "grants"]);
  const documentGrantRows = records(documentGrants.data, ["items", "grants"]);
  const updatePolicy = useMutation({ mutationFn: (policy: string) => controlApi.updateKbAccess(kbId, policy), onSuccess: async () => { toast.success("知识库策略已保存"); await queryClient.invalidateQueries({ queryKey: ["access", "kb-policy"] }); }, onError: (error) => toast.error(error.message) });
  const updateDocumentPolicy = useMutation({ mutationFn: (policy: string) => controlApi.updateDocumentAccess(kbId, activeDocumentId, policy), onSuccess: async () => { toast.success("文档策略已保存"); await queryClient.invalidateQueries({ queryKey: ["access", "document-policy"] }); }, onError: (error) => toast.error(error.message) });
  const revokeKb = useMutation({ mutationFn: (row: JsonRecord) => controlApi.revokeKbAccess(kbId, textValue(row.subject_id, "")), onSuccess: async () => { toast.success("知识库授权已撤销"); await queryClient.invalidateQueries({ queryKey: ["access", "kb-grants"] }); }, onError: (error) => toast.error(error.message) });
  const revokeDocument = useMutation({ mutationFn: (row: JsonRecord) => controlApi.revokeDocumentAccess(kbId, activeDocumentId, textValue(row.subject_id, "")), onSuccess: async () => { toast.success("文档授权已撤销"); await queryClient.invalidateQueries({ queryKey: ["access", "document-grants"] }); }, onError: (error) => toast.error(error.message) });
  const removeKnowledgeBase = useMutation({ mutationFn: () => api.deleteKnowledgeBase(kbId), onSuccess: async () => { toast.success("知识库已删除"); setSelectedKbId(null); await queryClient.invalidateQueries({ queryKey: queryKeys.knowledgeBases(workspaceId) }); router.replace("/home"); }, onError: (error) => toast.error(error.message) });
  return <div className="min-h-full"><PageHeader eyebrow="Resource ACL" title="访问权限" description="查看和修改知识库、文档的可见性策略与按主体授权。后端始终是最终权限裁决者。" /><div className="grid gap-6 p-4 md:p-6 xl:grid-cols-2">
    <section className="overflow-hidden border border-border bg-surface"><div className="flex items-start justify-between gap-4 border-b border-border px-4 py-3"><div><div className="flex items-center gap-2"><Shield className="size-4 text-primary" /><h3 className="text-[13px] font-semibold">知识库策略</h3></div><p className="mt-1 text-[11px] text-muted-foreground">{kbId}</p></div><Select value={textValue(kbPolicyRow.policy, "workspace")} onValueChange={(value) => updatePolicy.mutate(value)}><SelectTrigger className="w-36"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="workspace">工作区可见</SelectItem><SelectItem value="private">仅授权主体</SelectItem></SelectContent></Select></div><QueryState pending={kbGrants.isPending} error={kbGrants.error} onRetry={() => void kbGrants.refetch()} /><GrantList rows={kbGrantRows} onRevoke={(row) => revokeKb.mutate(row)} /><div className="border-t border-border p-3"><GrantDialog label="知识库" onGrant={async (subject, role) => { await controlApi.grantKbAccess(kbId, subject, role); await queryClient.invalidateQueries({ queryKey: ["access", "kb-grants"] }); }} /></div></section>
    <section className="overflow-hidden border border-border bg-surface"><div className="border-b border-border px-4 py-3"><div className="flex items-center gap-2"><LockKeyhole className="size-4 text-primary" /><h3 className="text-[13px] font-semibold">文档策略</h3></div><div className="mt-3 grid grid-cols-[minmax(0,1fr)_140px] gap-2"><Select value={activeDocumentId} onValueChange={setDocumentId}><SelectTrigger><SelectValue placeholder="选择文档" /></SelectTrigger><SelectContent>{(documents.data ?? []).map((document) => <SelectItem key={document.document_id} value={document.document_id}>{document.name}</SelectItem>)}</SelectContent></Select><Select value={textValue(documentPolicyRow.policy, "inherit")} disabled={!activeDocumentId} onValueChange={(value) => updateDocumentPolicy.mutate(value)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="inherit">继承知识库</SelectItem><SelectItem value="workspace">工作区可见</SelectItem><SelectItem value="private">仅授权主体</SelectItem></SelectContent></Select></div></div>{activeDocumentId ? <><QueryState pending={documentGrants.isPending} error={documentGrants.error} onRetry={() => void documentGrants.refetch()} /><GrantList rows={documentGrantRows} onRevoke={(row) => revokeDocument.mutate(row)} /><div className="border-t border-border p-3"><GrantDialog label="文档" onGrant={async (subject, role) => { await controlApi.grantDocumentAccess(kbId, activeDocumentId, subject, role); await queryClient.invalidateQueries({ queryKey: ["access", "document-grants"] }); }} /></div></> : <EmptyState icon={LockKeyhole} compact title="没有文档" description="上传文档后可设置独立策略与授权。" />}</section>
  </div>{canDelete ? <section className="mx-4 mb-6 border border-error/30 bg-surface md:mx-6"><div className="flex flex-col justify-between gap-3 px-4 py-3 sm:flex-row sm:items-center"><div><h3 className="text-[13px] font-semibold text-error">删除知识库</h3><p className="mt-1 text-xs text-muted-foreground">删除 {kbId} 的全部文档与索引。此操作不可恢复。</p></div><Dialog open={deleteOpen} onOpenChange={(open) => { setDeleteOpen(open); if (!open) setDeleteConfirmation(""); }}><DialogTrigger asChild><Button variant="destructive"><Trash2 className="size-4" />删除知识库</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>确认删除 {kbId}</DialogTitle><DialogDescription>这会永久删除知识库、文档和索引。输入知识库 ID 以继续。</DialogDescription></DialogHeader><div className="space-y-1.5"><Label htmlFor="delete-kb-confirmation">知识库 ID</Label><Input id="delete-kb-confirmation" value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} autoComplete="off" /></div><DialogFooter><Button variant="ghost" onClick={() => setDeleteOpen(false)}>取消</Button><Button variant="destructive" disabled={deleteConfirmation !== kbId} loading={removeKnowledgeBase.isPending} onClick={() => removeKnowledgeBase.mutate()}>永久删除</Button></DialogFooter></DialogContent></Dialog></div></section> : null}</div>;
}
