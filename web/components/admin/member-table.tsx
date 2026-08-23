import { Badge } from "@/components/ui/badge";
import { DataGrid, type DataGridColumn } from "@/components/data-display/data-grid";

export interface MemberRow {
  memberId: string;
  displayName: string;
  email: string;
  role: string;
  status?: string;
}

export function MemberTable({ members }: { members: MemberRow[] }) {
  const columns: DataGridColumn<MemberRow>[] = [
    { id: "member", header: "成员", cell: (row) => <div><div className="font-medium">{row.displayName}</div><div className="text-xs text-muted-foreground">{row.email}</div></div> },
    { id: "role", header: "角色", cell: (row) => <Badge>{row.role}</Badge> },
    { id: "status", header: "状态", cell: (row) => row.status ?? "Active" },
  ];
  return <DataGrid columns={columns} rows={members} rowKey={(row) => row.memberId} empty={<span className="text-muted-foreground">暂无成员</span>} />;
}
