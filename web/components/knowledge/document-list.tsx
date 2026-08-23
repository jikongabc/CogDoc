"use client";

import { useState } from "react";
import { File, FileText, Link2, Trash2 } from "lucide-react";
import type { Document } from "@/lib/api/types";
import { DataGrid, type DataGridColumn } from "@/components/data-display/data-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatFileType } from "@/lib/utils";

function shortId(value?: string) {
  if (!value) return "—";
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

export function DocumentList({
  documents,
  loading = false,
  onDelete,
}: {
  documents: Document[];
  loading?: boolean;
  onDelete?: (document: Document) => void;
}) {
  const [pendingDelete, setPendingDelete] = useState<Document | null>(null);
  const columns: DataGridColumn<Document>[] = [
    {
      id: "name",
      header: "文档",
      cell: (row) => <div className="flex min-w-64 items-center gap-2.5"><span className="flex size-7 shrink-0 items-center justify-center rounded-[4px] bg-surface-subtle text-muted-foreground">{row.kind === "url" ? <Link2 className="size-3.5" /> : <FileText className="size-3.5" />}</span><div className="min-w-0"><div className="truncate font-medium">{row.name}</div><div className="mt-0.5 truncate text-[11px] text-muted-foreground">{row.origin_uri || row.connector_type}</div></div></div>,
    },
    { id: "type", header: "类型", cell: (row) => <Badge>{formatFileType(row.media_type)}</Badge> },
    { id: "source", header: "来源", cell: (row) => <span className="text-muted-foreground">{row.connector_type === "legacy-upload" ? "直接上传" : row.connector_type}</span> },
    { id: "version", header: "版本", cell: (row) => <code className="font-mono text-[11px] text-muted-foreground">{shortId(row.version_id || row.sha256)}</code> },
  ];
  if (onDelete) {
    columns.push({
      id: "actions",
      header: "",
      cell: (row) => <Button variant="ghost" size="icon" className="text-error" onClick={() => setPendingDelete(row)} aria-label={`删除文档 ${row.name}`}><Trash2 className="size-4" /></Button>,
    });
  }
  if (loading) return <div className="flex h-40 items-center justify-center rounded-[5px] border border-border bg-surface text-sm text-muted-foreground">正在读取文档…</div>;
  return <><DataGrid columns={columns} rows={documents} rowKey={(row) => row.document_id || row.name} empty={<div className="mx-auto max-w-sm"><File className="mx-auto mb-2 size-5 text-muted-foreground" /><p className="font-medium text-foreground">还没有文档</p><p className="mt-1 text-xs text-muted-foreground">上传第一个文件，入库完成后即可在对话中引用。</p></div>} /><Dialog open={Boolean(pendingDelete)} onOpenChange={(open) => !open && setPendingDelete(null)}><DialogContent><DialogHeader><DialogTitle>删除文档</DialogTitle><DialogDescription>将删除“{pendingDelete?.name}”及其当前索引内容。已有审计和引用记录仍按后端保留策略处理。</DialogDescription></DialogHeader><DialogFooter><Button variant="ghost" onClick={() => setPendingDelete(null)}>取消</Button><Button variant="destructive" onClick={() => { if (pendingDelete) onDelete?.(pendingDelete); setPendingDelete(null); }}>确认删除</Button></DialogFooter></DialogContent></Dialog></>;
}
