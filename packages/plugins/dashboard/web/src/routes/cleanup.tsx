import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { applyCleanup, fetchCleanupPreview, type CleanupTarget } from "../api";
import { RouteHeader } from "../components/route-header";
import { StateBlock } from "../components/state-block";
import { ReasonBadges, ReasonSummary } from "../components/badges";
import { useToast } from "../components/toast";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { TableSkeleton } from "../components/ui/skeleton";
import { ShieldAlert, RefreshCcw } from "lucide-react";

export function CleanupRoute() {
  return (
    <div className="route-stack">
      <RouteHeader eyebrow="Safety first" title="Preview cleanup candidates, choose targets, then explicitly trash or delete." />
      <section className="split-grid">
        <CleanupPanel kind="project" title="Project Cleanup" description="Old or missing project paths plus stale provider metadata." />
        <CleanupPanel kind="session" title="Session Cleanup" description="Empty session logs that have no useful user-visible records." />
      </section>
    </div>
  );
}

function CleanupPanel({ kind, title, description }: { kind: "project" | "session"; title: string; description: string }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [selected, setSelected] = React.useState<Set<string>>(() => new Set());
  const [action, setAction] = React.useState<"trash" | "delete">("trash");
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  const preview = useQuery({
    queryKey: ["cleanup", kind],
    queryFn: () => fetchCleanupPreview(kind),
  });

  const apply = useMutation({
    mutationFn: () =>
      applyCleanup(kind, {
        action,
        paths: Array.from(selected),
        filters: preview.data?.filters ?? {},
      }),
    onSuccess: (result) => {
      setSelected(new Set());
      setConfirmOpen(false);
      toast(`Applied ${result.action} to ${result.summary.target_count} item(s).`, "success");
      void queryClient.invalidateQueries({ queryKey: ["cleanup", kind] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
    onError: (error: Error) => {
      toast(`Cleanup failed: ${error.message}`, "error");
    },
  });

  const candidates = preview.data?.candidates ?? [];
  const allSelected = candidates.length > 0 && selected.size === candidates.length;

  function toggle(path: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(candidates.map((item) => item.path)));
  }

  function handleConfirm() {
    apply.mutate();
  }

  return (
    <Card className="cleanup-panel panel-surface">
      <CardHeader>
        <div className="panel-title-row">
          <div>
            <CardTitle>{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
          <Badge variant={preview.data?.summary.candidate_count ? "risk" : "quiet"}>
            {preview.data?.summary.candidate_count ?? 0} candidates
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        {preview.isPending ? <TableSkeleton rows={4} cols={3} /> : null}
        {preview.isError ? <StateBlock title="Cleanup preview failed" detail={preview.error.message} /> : null}
        {preview.data ? (
          <>
            <div className="cleanup-actions">
              <Button variant="secondary" size="sm" onClick={() => void preview.refetch()}>
                <RefreshCcw size={15} /> Refresh
              </Button>
              <label className="select-field">
                <span>Action</span>
                <select value={action} onChange={(event) => setAction(event.target.value as "trash" | "delete")}>
                  <option value="trash">Trash</option>
                  <option value="delete">Delete</option>
                </select>
              </label>
              <Button
                variant={action === "delete" ? "destructive" : "default"}
                size="sm"
                disabled={!selected.size || apply.isPending}
                onClick={() => setConfirmOpen(true)}
              >
                <ShieldAlert size={15} /> Apply to {selected.size}
              </Button>
            </div>
            <div className="table-shell compact-scroll">
              <Table>
                <TableHead>
                  <TableRow>
                    <TableHeader>
                      <input type="checkbox" aria-label={`Select all ${kind} cleanup candidates`} checked={allSelected} onChange={toggleAll} />
                    </TableHeader>
                    <TableHeader>Target</TableHeader>
                    <TableHeader>Reason</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {candidates.map((candidate) => (
                    <TableRow key={candidate.path}>
                      <TableCell>
                        <input type="checkbox" aria-label={`Select ${candidate.path}`} checked={selected.has(candidate.path)} onChange={() => toggle(candidate.path)} />
                      </TableCell>
                      <TableCell>
                        <TargetLabel target={candidate} />
                      </TableCell>
                      <TableCell>
                        <ReasonBadges reasons={candidate.reason} />
                      </TableCell>
                    </TableRow>
                  ))}
                  {!candidates.length ? (
                    <TableRow>
                      <TableCell colSpan={3}>No cleanup candidates.</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
            <ReasonSummary title="Skipped reasons" reasons={preview.data.summary.skipped_reasons} />
          </>
        ) : null}
      </CardContent>
      <ConfirmDialog
        open={confirmOpen}
        title={`Confirm ${action}`}
        description={`This will ${action} ${selected.size} selected ${kind} item(s). ${action === "delete" ? "This action cannot be undone." : "Items will be moved to trash."}`}
        confirmLabel={`${action === "delete" ? "Delete" : "Trash"} ${selected.size} item(s)`}
        variant={action === "delete" ? "destructive" : "default"}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmOpen(false)}
      />
    </Card>
  );
}

function TargetLabel({ target }: { target: CleanupTarget }) {
  return (
    <div className="target-label">
      <strong>{target.project ?? target.vendor ?? "target"}</strong>
      <span>{target.path}</span>
    </div>
  );
}
