import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./table";

export interface DataGridColumn<T> {
  id: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  className?: string;
}

interface DataGridProps<T> {
  columns: DataGridColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  empty: ReactNode;
  className?: string;
}

export function DataGrid<T>({ columns, rows, rowKey, empty, className }: DataGridProps<T>) {
  return (
    <div className={cn("overflow-hidden rounded-[5px] border border-border bg-surface", className)}>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-surface-subtle">
              {columns.map((column) => <TableHead key={column.id} className={column.className}>{column.header}</TableHead>)}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={rowKey(row)}>
                {columns.map((column) => <TableCell key={column.id} className={column.className}>{column.cell(row)}</TableCell>)}
              </TableRow>
            ))}
            {!rows.length ? <TableRow className="hover:bg-surface"><TableCell colSpan={columns.length} className="h-40 text-center">{empty}</TableCell></TableRow> : null}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
