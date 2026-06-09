import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
  type RowSelectionState,
} from "@tanstack/react-table";
import { applyCleanup, fetchCleanupPreview, type CleanupTarget } from "@/api";
import { RouteHeader } from "@/components/route-header";
import { StateBlock } from "@/components/state-block";
import { ReasonBadges, ReasonSummary } from "@/components/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableSkeleton } from "@/components/ui/skeleton";
import { ShieldAlert, RefreshCcw } from "lucide-react";

export function CleanupRoute() {
  return (
    <div className="mx-auto grid max-w-[96rem] gap-5">
      <RouteHeader eyebrow="Safety first" title="Preview cleanup candidates, choose targets, then explicitly confirm the action." />
      <section className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <CleanupPanel kind="project" title="Project Cleanup" description="Old or missing project paths plus stale provider metadata." />
        <CleanupPanel kind="session" title="Session Cleanup" description="Empty session logs that have no useful user-visible records." />
      </section>
    </div>
  );
}

function makeColumns(kind: "project" | "session"): ColumnDef<CleanupTarget>[] {
  return [
    {
      id: "select",
      header: ({ table }) => (
        <Checkbox
          checked={table.getIsAllPageRowsSelected() || (table.getIsSomePageRowsSelected() && "indeterminate")}
          onCheckedChange={(value) => table.toggleAllPageRowsSelected(!!value)}
          aria-label={`Select all ${kind} cleanup candidates`}
        />
      ),
      cell: ({ row }) => (
        <Checkbox
          checked={row.getIsSelected()}
          onCheckedChange={(value) => row.toggleSelected(!!value)}
          aria-label={`Select ${row.original.path}`}
        />
      ),
      enableSorting: false,
      size: 40,
    },
    {
      id: "target",
      header: () => <span className="font-extrabold uppercase tracking-[0.08em]">Target</span>,
      cell: ({ row }) => <TargetLabel target={row.original} />,
    },
    {
      id: "reason",
      header: () => <span className="font-extrabold uppercase tracking-[0.08em]">Reason</span>,
      cell: ({ row }) => <ReasonBadges reasons={row.original.reason} />,
    },
  ];
}

function CleanupPanel({ kind, title, description }: { kind: "project" | "session"; title: string; description: string }) {
  const queryClient = useQueryClient();
  const [rowSelection, setRowSelection] = React.useState<RowSelectionState>({});
  const [sessionAction, setSessionAction] = React.useState<"trash" | "delete">("trash");
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const action = kind === "project" ? "delete" : sessionAction;
  const columns = React.useMemo(() => makeColumns(kind), [kind]);

  const preview = useQuery({
    queryKey: ["cleanup", kind],
    queryFn: () => fetchCleanupPreview(kind),
  });

  const candidates = preview.data?.candidates ?? [];

  const table = useReactTable({
    data: candidates,
    columns,
    state: { rowSelection },
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.path,
    enableRowSelection: true,
  });

  const selectedPaths = Object.keys(rowSelection).filter((key) => rowSelection[key]);
  const selectedCount = selectedPaths.length;

  React.useEffect(() => {
    setRowSelection({});
  }, [candidates.length]);

  const apply = useMutation({
    mutationFn: () =>
      applyCleanup(kind, {
        action,
        paths: selectedPaths,
        filters: preview.data?.filters ?? {},
      }),
    onSuccess: (result) => {
      setRowSelection({});
      setConfirmOpen(false);
      toast.success(`Applied ${result.action} to ${result.summary.target_count} item(s).`);
      void queryClient.invalidateQueries({ queryKey: ["cleanup", kind] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
    onError: (error: Error) => {
      toast.error(`Cleanup failed: ${error.message}`);
    },
  });

  function handleConfirm() {
    apply.mutate();
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3 max-[32rem]:flex-col max-[32rem]:items-stretch">
          <div>
            <CardTitle className="font-display text-xl tracking-tight">{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
          <Badge variant={preview.data?.summary.candidate_count ? "destructive" : "secondary"}>
            {preview.data?.summary.candidate_count ?? 0} candidates
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        {preview.isPending ? <TableSkeleton rows={4} cols={3} /> : null}
        {preview.isError ? <StateBlock title="Cleanup preview failed" detail={preview.error.message} /> : null}
        {preview.data ? (
          <>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3 max-[32rem]:flex-col max-[32rem]:items-stretch">
              <Button variant="secondary" size="sm" onClick={() => void preview.refetch()}>
                <RefreshCcw size={15} /> Refresh
              </Button>
              {kind === "session" ? (
                <div className="grid min-w-[8rem] gap-1">
                  <span className="font-display text-[0.82rem] font-extrabold uppercase tracking-[0.08em] text-muted-foreground">Action</span>
                  <Select value={sessionAction} onValueChange={(v) => setSessionAction(v as "trash" | "delete")}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="trash">Trash</SelectItem>
                      <SelectItem value="delete">Delete</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              ) : null}
              <Button
                variant={action === "delete" ? "destructive" : "default"}
                size="sm"
                disabled={!selectedCount || apply.isPending}
                onClick={() => setConfirmOpen(true)}
              >
                <ShieldAlert size={15} /> {kind === "project" ? "Delete" : "Apply to"} {selectedCount}
              </Button>
            </div>
            <div className="max-h-96 overflow-auto rounded-[1.2rem] border border-foreground/13 dark:border-[rgb(255_255_255/8%)]">
              <Table>
                <TableHead className="sticky top-0 z-1 bg-[#eee0bd] font-display text-[0.8rem] uppercase tracking-[0.08em] dark:bg-[#2a2620]">
                  {table.getHeaderGroups().map((headerGroup) => (
                    <TableRow key={headerGroup.id}>
                      {headerGroup.headers.map((header) => (
                        <TableHeader key={header.id} className={header.id === "select" ? "w-10" : undefined}>
                          {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                        </TableHeader>
                      ))}
                    </TableRow>
                  ))}
                </TableHead>
                <TableBody>
                  {table.getRowModel().rows.map((row) => (
                    <TableRow key={row.id} data-state={row.getIsSelected() && "selected"}>
                      {row.getVisibleCells().map((cell) => (
                        <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                  {!table.getRowModel().rows.length ? (
                    <TableRow>
                      <TableCell colSpan={columns.length}>No cleanup candidates.</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
            <ReasonSummary title="Skipped reasons" reasons={preview.data.summary.skipped_reasons} />
          </>
        ) : null}
      </CardContent>
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Confirm {action}</AlertDialogTitle>
            <AlertDialogDescription>
              This will {action} {selectedCount} selected {kind} item(s). {action === "delete" ? "This action cannot be undone." : "Items will be moved to trash."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction variant={action === "delete" ? "destructive" : "default"} onClick={handleConfirm}>
              {action === "delete" ? "Delete" : "Trash"} {selectedCount} item(s)
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

function TargetLabel({ target }: { target: CleanupTarget }) {
  return (
    <div className="grid gap-1">
      <strong>{target.project ?? target.vendor ?? "target"}</strong>
      <span className="break-words font-mono text-[0.83rem] text-muted-foreground">{target.path}</span>
    </div>
  );
}
