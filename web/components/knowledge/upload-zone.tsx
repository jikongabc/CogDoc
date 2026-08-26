"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Cloud, Cpu, FileText, FileUp, Files, Plus, RotateCcw, UploadCloud, X } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api/client";
import type { EmbeddingProfile, IndexJob } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { BUILT_IN_WORKSPACE_ROLES, RoleSelector } from "@/components/access/role-selector";
import { CreateWorkspaceRoleDialog } from "@/components/access/create-role-dialog";

const ACCEPTED_EXTENSIONS = ["pdf", "md", "markdown", "txt", "html", "htm", "docx", "pptx", "xlsx", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"];
const MAX_FILES = 20;

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function providerIcon(profile: EmbeddingProfile) {
  return profile.kind === "local" ? Cpu : Cloud;
}

export function UploadZone({ kbId }: { kbId: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const [dragging, setDragging] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const restoredStorageKeyRef = useRef<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [selectedRoleIds, setSelectedRoleIds] = useState<string[] | null>(null);
  const [embeddingProfileSelection, setEmbeddingProfileSelection] = useState<"local" | "cloud" | null>(null);

  const rolesQuery = useQuery({
    queryKey: queryKeys.workspaceRoles(workspaceId),
    queryFn: () => api.workspaceRoles(workspaceId as string),
    enabled: Boolean(workspaceId),
  });
  const profilesQuery = useQuery({ queryKey: queryKeys.embeddingProfiles, queryFn: api.embeddingProfiles });
  const knowledgeBasesQuery = useQuery({ queryKey: queryKeys.knowledgeBases(workspaceId), queryFn: api.knowledgeBases });
  const knowledgeBase = knowledgeBasesQuery.data?.find((item) => item.kb_id === kbId);
  const activeProfileId = knowledgeBase?.embedding_profile_id ?? "local";
  const embeddingProfileId = embeddingProfileSelection ?? activeProfileId;
  const availableRoles = rolesQuery.data?.roles ?? BUILT_IN_WORKSPACE_ROLES;
  const profiles = profilesQuery.data ?? [];

  const jobQuery = useQuery({
    queryKey: queryKeys.indexJob(workspaceId, jobId || "none"),
    queryFn: () => api.indexJob(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = (query.state.data as IndexJob | undefined)?.status;
      return status === "succeeded" || status === "failed" ? false : 1800;
    },
  });

  const storageKey = `cogdoc.index-job.v1:${workspaceId || "legacy"}:${kbId}`;
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      restoredStorageKeyRef.current = storageKey;
      setJobId(sessionStorage.getItem(storageKey));
    });
    return () => cancelAnimationFrame(frame);
  }, [storageKey]);

  useEffect(() => {
    if (restoredStorageKeyRef.current !== storageKey) return;
    if (jobId) sessionStorage.setItem(storageKey, jobId);
    else sessionStorage.removeItem(storageKey);
  }, [jobId, storageKey]);

  useEffect(() => {
    if (jobQuery.data?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents(workspaceId, kbId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.knowledgeBases(workspaceId) });
    }
  }, [jobQuery.data?.status, kbId, queryClient, workspaceId]);

  const acceptFiles = useCallback((candidates: File[]) => {
    if (!candidates.length) return;
    setJobId(null);
    setUploadError(null);
    setFiles((current) => {
      const known = new Set(current.map((file) => file.name));
      const next = [...current];
      let rejected = 0;
      for (const candidate of candidates) {
        const extension = candidate.name.split(".").pop()?.toLowerCase() || "";
        if (!ACCEPTED_EXTENSIONS.includes(extension) || known.has(candidate.name)) {
          rejected += 1;
          continue;
        }
        if (next.length >= MAX_FILES) break;
        known.add(candidate.name);
        next.push(candidate);
      }
      if (rejected) {
        queueMicrotask(() => setUploadError("已跳过不支持的格式或同名文件。"));
      } else if (current.length + candidates.length > MAX_FILES) {
        queueMicrotask(() => setUploadError(`一次最多上传 ${MAX_FILES} 个文件。`));
      }
      return next;
    });
  }, []);

  const upload = async () => {
    if (!files.length) return;
    const roleIds = selectedRoleIds ?? availableRoles.map((role) => role.role_id);
    if (!roleIds.length) {
      setUploadError("请至少选择一个可访问角色");
      return;
    }
    const selectedProfile = profiles.find((profile) => profile.profile_id === embeddingProfileId);
    if (selectedProfile && !selectedProfile.available) {
      setUploadError("云端 Embedding 尚未由管理员配置");
      return;
    }
    setUploading(true);
    setUploadError(null);
    try {
      const result = await api.uploadDocuments(kbId, files, roleIds, embeddingProfileId);
      setJobId(result.job_id);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
  };

  const reset = () => {
    setFiles([]);
    setJobId(null);
    setUploadError(null);
    setSelectedRoleIds(null);
    setEmbeddingProfileSelection(null);
    if (inputRef.current) inputRef.current.value = "";
  };
  const removeFile = (name: string) => {
    setFiles((current) => current.filter((file) => file.name !== name));
    if (inputRef.current) inputRef.current.value = "";
  };

  const job = jobQuery.data;
  const active = uploading || Boolean(jobId && (jobQuery.isPending || job?.status === "pending" || job?.status === "running"));
  const activeLabel = uploading ? `正在上传 ${files.length} 个文件` : job?.status === "pending" ? "等待入库处理" : "正在解析、切分并建立索引";
  const totalBytes = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files]);
  const switchingModel = Boolean(knowledgeBase?.document_count && embeddingProfileId !== activeProfileId);

  return (
    <div className="space-y-4">
      <section aria-labelledby="embedding-provider-label" className="border-y border-border bg-surface">
        <div className="flex flex-col gap-3 px-3 py-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p id="embedding-provider-label" className="text-[13px] font-medium">Embedding 模型</p>
            <p className="mt-0.5 text-xs text-muted-foreground">模型配置绑定知识库，索引和检索始终使用同一向量空间。</p>
          </div>
          <div role="radiogroup" aria-labelledby="embedding-provider-label" className="grid min-w-0 grid-cols-2 border border-border sm:w-[360px]">
            {profiles.length ? profiles.map((profile) => {
              const Icon = providerIcon(profile);
              const selected = embeddingProfileId === profile.profile_id;
              return (
                <button
                  key={profile.profile_id}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  disabled={!profile.available || active}
                  title={profile.description}
                  onClick={() => setEmbeddingProfileSelection(profile.profile_id)}
                  className={cn(
                    "flex min-w-0 items-center gap-2 px-3 py-2 text-left text-xs transition-colors first:border-r first:border-border",
                    selected ? "bg-surface-subtle text-foreground" : "bg-surface text-muted-foreground hover:bg-surface-subtle",
                    (!profile.available || active) && "cursor-not-allowed opacity-50",
                  )}
                >
                  <Icon className="size-4 shrink-0" />
                  <span className="min-w-0"><span className="block truncate font-medium">{profile.kind === "local" ? "本地" : "云端"}</span><span className="block truncate text-[10px] text-muted-foreground">{profile.model || "模型未配置"}{!profile.available ? " · 不可用" : ""}</span></span>
                </button>
              );
            }) : <><div className="h-[52px] animate-pulse bg-surface-subtle" /><div className="h-[52px] animate-pulse bg-surface-subtle" /></>}
          </div>
        </div>
        {switchingModel ? <div className="border-t border-warning/30 bg-warning-subtle px-3 py-2 text-xs text-warning"><AlertTriangle className="mr-1.5 inline size-3.5" />切换模型会重新生成整个知识库的向量索引，已有文档不会丢失。</div> : null}
      </section>

      <div
        className={cn("flex min-h-32 items-center justify-center rounded-[6px] border border-dashed border-border-strong bg-surface px-5 py-5 text-center transition-colors", dragging && "border-primary bg-primary-subtle", files.length && "min-h-0 border-solid")}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => { event.preventDefault(); setDragging(false); }}
        onDrop={(event) => { event.preventDefault(); setDragging(false); acceptFiles(Array.from(event.dataTransfer.files)); }}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          className="sr-only"
          tabIndex={-1}
          aria-label="选择要上传的文档"
          onChange={(event) => acceptFiles(Array.from(event.target.files ?? []))}
          accept={ACCEPTED_EXTENSIONS.map((item) => `.${item}`).join(",")}
        />
        {!files.length && !jobId ? <div><span className="mx-auto flex size-9 items-center justify-center rounded-[5px] border border-border bg-surface-subtle text-muted-foreground"><UploadCloud className="size-[18px]" /></span><p className="mt-3 text-sm font-medium">拖放一个或多个文件到这里</p><p className="mt-1 text-xs text-muted-foreground">一次最多 {MAX_FILES} 个；支持 PDF、Office、Markdown、HTML、文本与图片</p><Button variant="ghost" size="compact" className="mt-2 text-primary" onClick={() => inputRef.current?.click()}>选择文件</Button></div> : (
          <div className="w-full text-left">
            <div className="flex items-center justify-between gap-3 border-b border-border pb-2">
              <div className="flex min-w-0 items-center gap-2"><Files className="size-4 text-muted-foreground" /><p className="text-[13px] font-medium">{files.length ? `${files.length} 个文件` : "已恢复入库任务"}</p>{files.length ? <span className="text-xs text-muted-foreground">{formatBytes(totalBytes)}</span> : null}</div>
              {!active && !job ? <Button variant="ghost" size="compact" onClick={() => inputRef.current?.click()}><Plus className="size-3.5" />继续添加</Button> : null}
            </div>
            {files.length ? <div className="max-h-56 divide-y divide-border overflow-y-auto">
              {files.map((file) => (
                <div key={file.name} className="flex items-center gap-3 py-2.5">
                  <FileText className="size-4 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1"><p className="truncate text-[13px] font-medium">{file.name}</p><p className="mt-0.5 text-[11px] text-muted-foreground">{formatBytes(file.size)}</p></div>
                  <span className="text-[11px] text-muted-foreground">{uploading ? "上传中" : active ? "处理中" : job?.status === "succeeded" ? "已完成" : job?.status === "failed" ? "失败" : "待上传"}</span>
                  {!active && !job ? <Button variant="ghost" size="icon" onClick={() => removeFile(file.name)} aria-label={`移除 ${file.name}`}><X className="size-3.5" /></Button> : null}
                </div>
              ))}
            </div> : null}
            {active ? <div className="border-t border-border pt-3"><div className="mb-2 flex items-center justify-between gap-3 text-[13px]"><span className="font-medium text-foreground">{activeLabel}</span><span className="text-xs text-muted-foreground">处理中</span></div><Progress label={activeLabel} /><p className="mt-2 text-xs text-muted-foreground">{uploading ? "文件上传完成后会自动开始一轮统一入库。" : "完成后会自动刷新文档列表；无需停留在当前页面。"}</p></div> : null}
            {job?.status === "succeeded" ? <div role="status" aria-live="polite" className="flex items-center justify-between gap-3 border-t border-border pt-3"><div className="flex items-center gap-2 text-[13px] text-success"><CheckCircle2 className="size-4" />入库完成 · {job.document_count ?? 0} 个文档 / {job.chunk_count ?? 0} 个片段</div><Button variant="ghost" size="compact" onClick={reset}>继续上传</Button></div> : null}
            {job?.status === "failed" ? <div role="alert" className="mt-3 border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error"><div className="flex items-center gap-2 font-medium"><AlertTriangle className="size-4" />入库失败</div><p className="mt-1">{job.message || "文档处理未完成，请重试。"}</p>{files.length ? <Button variant="ghost" size="compact" className="mt-1 text-error" loading={uploading} onClick={() => { setJobId(null); void upload(); }}><RotateCcw className="size-3.5" />重试全部</Button> : <Button variant="ghost" size="compact" className="mt-1 text-error" onClick={reset}><RotateCcw className="size-3.5" />重新选择文件</Button>}</div> : null}
          </div>
        )}
      </div>

      {files.length && !job ? <div className="space-y-1.5"><div className="flex items-center justify-between gap-3"><p className="text-[13px] font-medium">文档可访问角色</p><div className="flex items-center gap-2"><span className="text-[11px] text-muted-foreground">应用到本批全部文件</span><CreateWorkspaceRoleDialog workspaceId={workspaceId || ""} triggerVariant="ghost" /></div></div><RoleSelector roles={availableRoles} selected={selectedRoleIds ?? availableRoles.map((role) => role.role_id)} onChange={setSelectedRoleIds} compact disabled={uploading} /><p className="text-xs text-muted-foreground">入库后，只有选中的角色能在文档列表和 RAG 结果中看到这些文件。</p></div> : null}
      {uploadError ? <div role="alert" className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{uploadError}</div> : null}
      {files.length && !job ? <div className="flex justify-end"><Button variant="primary" loading={uploading} onClick={() => void upload()}><FileUp className="size-4" />{uploading ? "正在上传" : switchingModel ? "上传并重建索引" : `上传 ${files.length} 个文件`}</Button></div> : null}
      {jobQuery.isError ? <div className="flex items-center justify-between border-l-2 border-warning bg-warning-subtle px-3 py-2 text-[13px] text-warning"><span>暂时无法读取入库状态。</span><Button variant="ghost" size="compact" onClick={() => { void jobQuery.refetch(); toast.info("正在重新查询状态"); }}>重试</Button></div> : null}
    </div>
  );
}
