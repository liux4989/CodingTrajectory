import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  useReactTable,
  getCoreRowModel,
  type ColumnDef,
  type RowSelectionState,
} from "@tanstack/react-table";
import { applyCleanup, fetchCleanupPreview, type CleanupTarget } from "@/api";
import { useDateRange } from "@/hooks/use-date-range";
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
import { TableSkeleton } from "@/components/ui/skeleton";
import { DataTable } from "@/components/data-table";
import { ShieldAlert } from "lucide-react";

export function CleanupRoute() {
  return (
    <div className="route-container">
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
      header: () => <span className="label-uppercase">Target</span>,
      cell: ({ row }) => <TargetLabel target={row.original} />,
    },
    {
      id: "reason",
      header: () => <span className="label-uppercase">Reason</span>,
      cell: ({ row }) => <ReasonBadges reasons={row.original.reason} />,
    },
  ];
}

function CleanupPanel({ kind, title, description }: { kind: "project" | "session"; title: string; description: string }) {
  const queryClient = useQueryClient();
  const { days: sinceDays } = useDateRange();
  const [rowSelection, setRowSelection] = React.useState<RowSelectionState>({});
  const [sessionAction, setSessionAction] = React.useState<"trash" | "delete">("trash");
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const action = kind === "project" ? "delete" : sessionAction;
  const columns = React.useMemo(() => makeColumns(kind), [kind]);

  const preview = useQuery({
    queryKey: ["cleanup", kind, sinceDays],
    queryFn: () => fetchCleanupPreview(kind, { sinceDays }),
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
            <CardTitle className="title-card">{title}</CardTitle>
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
              {kind === "session" ? (
                <div className="grid min-w-[8rem] gap-1">
                  <span className="eyebrow-soft text-muted-foreground">Action</span>
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
            <DataTable
              table={table}
              columnCount={columns.length}
              emptyMessage="No cleanup candidates."
              className="max-h-96 bg-transparent"
            />
            <ReasonSummary title="Skipped reasons" reasons={preview.data.summary.skipped_reasons} />
            <SkippedTargetsList skipped={preview.data.skipped} />
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

type SkippedTarget = {
  kind: string;
  path: string;
  reason: string[];
};

function SkippedTargetsList({ skipped }: { skipped: SkippedTarget[] }) {
  const groups = React.useMemo(() => groupSkippedTargets(skipped), [skipped]);
  if (!skipped.length) return null;

  return (
    <div className="mt-4 grid gap-2">
      <h3 className="m-0 font-display font-semibold">Skipped items</h3>
      <div className="grid gap-2">
        {groups.map((group) => (
          <details key={group.reason} className="rounded-md border border-border-soft bg-muted/20">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-body-sm font-medium marker:hidden">
              <span className="min-w-0 truncate">{group.reason}</span>
              <Badge variant="secondary" className="shrink-0">
                {group.items.length}
              </Badge>
            </summary>
            <div className="grid max-h-56 gap-2 overflow-auto border-t border-border-soft p-3">
              {group.items.map((item) => (
                <div key={`${group.reason}:${item.kind}:${item.path}`} className="grid gap-1 rounded-md bg-background/70 p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{item.kind}</Badge>
                    {item.reason.length > 1 ? <ReasonBadges reasons={item.reason.filter((reason) => reason !== group.reason)} /> : null}
                  </div>
                  <span className="break-words mono text-caption text-muted-foreground">{item.path}</span>
                </div>
              ))}
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}

function groupSkippedTargets(skipped: SkippedTarget[]) {
  const grouped = new Map<string, SkippedTarget[]>();
  for (const item of skipped) {
    const reasons = item.reason.length ? item.reason : ["unknown"];
    for (const reason of reasons) {
      const items = grouped.get(reason) ?? [];
      items.push(item);
      grouped.set(reason, items);
    }
  }
  return Array.from(grouped.entries())
    .map(([reason, items]) => ({
      reason,
      items: [...items].sort((left, right) => left.path.localeCompare(right.path)),
    }))
    .sort((left, right) => left.reason.localeCompare(right.reason));
}

function TargetLabel({ target }: { target: CleanupTarget }) {
  return (
    <div className="grid gap-1">
      <strong>{target.project ?? target.vendor ?? "target"}</strong>
      <span className="break-words mono text-caption text-muted-foreground">{target.path}</span>
    </div>
  );
}
