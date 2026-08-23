"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileUp, LoaderCircle, RotateCcw, UploadCloud, X } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api/client";
import type { IndexJob } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const ACCEPTED_EXTENSIONS = ["pdf", "md", "markdown", "txt", "html", "htm", "docx", "pptx", "xlsx", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"];

export function UploadZone({ kbId }: { kbId: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const restoredStorageKeyRef = useRef<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
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

  const acceptFile = useCallback((candidate?: File) => {
    if (!candidate) return;
    const extension = candidate.name.split(".").pop()?.toLowerCase() || "";
    if (!ACCEPTED_EXTENSIONS.includes(extension)) {
      setUploadError("此文件类型暂不支持。请选择文档、表格、演示文稿或图片。");
      return;
    }
    setFile(candidate);
    setJobId(null);
    setUploadError(null);
  }, []);

  const upload = async () => {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      const result = await api.uploadDocument(kbId, file);
      setJobId(result.job_id);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
  };

  const reset = () => { setFile(null); setJobId(null); setUploadError(null); if (inputRef.current) inputRef.current.value = ""; };
  const job = jobQuery.data;
  const active = uploading || Boolean(jobId && (jobQuery.isPending || job?.status === "pending" || job?.status === "running"));

  return (
    <div className="space-y-3">
      <div
        className={cn("flex min-h-36 items-center justify-center rounded-[8px] border border-dashed border-border-strong bg-surface px-5 py-6 text-center transition-colors", dragging && "border-primary bg-primary-subtle", file && "border-solid")}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => { event.preventDefault(); setDragging(false); }}
        onDrop={(event) => { event.preventDefault(); setDragging(false); acceptFile(event.dataTransfer.files[0]); }}
      >
        <input ref={inputRef} type="file" className="sr-only" tabIndex={-1} aria-label="选择要上传的文档" onChange={(event) => acceptFile(event.target.files?.[0])} accept={ACCEPTED_EXTENSIONS.map((item) => `.${item}`).join(",")} />
        {!file && !jobId ? <div><span className="mx-auto flex size-9 items-center justify-center rounded-[5px] border border-border bg-surface-subtle text-muted-foreground"><UploadCloud className="size-[18px]" /></span><p className="mt-3 text-sm font-medium">拖放文件到这里</p><p className="mt-1 text-xs text-muted-foreground">PDF、Office、Markdown、HTML、文本与图片</p><Button variant="ghost" size="compact" className="mt-2 text-primary" onClick={() => inputRef.current?.click()}>选择文件</Button></div> : <div className="w-full max-w-md text-left"><div className="flex items-center gap-3"><span className="flex size-9 shrink-0 items-center justify-center rounded-[5px] bg-primary-subtle text-primary"><FileUp className="size-[18px]" /></span><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{file?.name || "上次上传的文档"}</p>{file ? <p className="mt-0.5 text-xs text-muted-foreground">{Math.max(1, Math.round(file.size / 1024))} KB</p> : <p className="mt-0.5 text-xs text-muted-foreground">已恢复入库任务</p>}</div>{!active && !job ? <Button variant="ghost" size="icon" onClick={reset} aria-label="移除文件"><X className="size-4" /></Button> : null}</div>
          {active ? <div role="status" aria-live="polite" className="mt-4 flex items-center gap-2 border-t border-border pt-3 text-[13px] text-muted-foreground"><LoaderCircle className="size-4 animate-spin text-primary" />{uploading ? "正在上传" : job?.status === "pending" ? "等待入库" : "正在解析并建立索引"}</div> : null}
          {job?.status === "succeeded" ? <div role="status" aria-live="polite" className="mt-4 flex items-center justify-between gap-3 border-t border-border pt-3"><div className="flex items-center gap-2 text-[13px] text-success"><CheckCircle2 className="size-4" />入库完成 · {job.document_count ?? 0} 个文档 / {job.chunk_count ?? 0} 个片段</div><Button variant="ghost" size="compact" onClick={reset}>继续上传</Button></div> : null}
          {job?.status === "failed" ? <div role="alert" className="mt-4 border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error"><div className="flex items-center gap-2 font-medium"><AlertTriangle className="size-4" />入库失败</div><p className="mt-1">{job.message || "文档处理未完成，请重试。"}</p>{file ? <Button variant="ghost" size="compact" className="mt-1 text-error" onClick={() => { setJobId(null); void upload(); }}><RotateCcw className="size-3.5" />重试</Button> : <Button variant="ghost" size="compact" className="mt-1 text-error" onClick={reset}><RotateCcw className="size-3.5" />重新选择文件</Button>}</div> : null}
        </div>}
      </div>
      {uploadError ? <div role="alert" className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{uploadError}</div> : null}
      {file && !job && !active ? <div className="flex justify-end"><Button variant="primary" onClick={upload}><FileUp className="size-4" />上传并入库</Button></div> : null}
      {jobQuery.isError ? <div className="flex items-center justify-between border-l-2 border-warning bg-warning-subtle px-3 py-2 text-[13px] text-warning"><span>暂时无法读取入库状态。</span><Button variant="ghost" size="compact" onClick={() => { void jobQuery.refetch(); toast.info("正在重新查询状态"); }}>重试</Button></div> : null}
    </div>
  );
}
