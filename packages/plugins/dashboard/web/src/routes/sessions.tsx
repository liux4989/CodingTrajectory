import * as React from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchSessions, fetchOverview, type SessionItem } from "../api";
import { TableSkeleton } from "../components/ui/skeleton";
import { RouteHeader } from "../components/route-header";
import { Toolbar } from "../components/toolbar";
import { StateBlock } from "../components/state-block";
import { VendorBadges } from "../components/badges";
import { SortableHeader, useSort } from "../components/sortable-header";
import { Pagination } from "../components/pagination";
import { RefreshButton } from "../components/refresh-button";
import { Button } from "../components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { relativeTime } from "../lib/relative-time";
import { Trash2 } from "lucide-react";

const PAGE_SIZE = 20;

function sessionId(item: SessionItem) {
  return item.root_session_id ?? item.id ?? null;
}

function sessionVendors(item: SessionItem) {
  return item.vendors ?? item.v ?? [];
}

function shortId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function getSortValue(item: SessionItem, key: string) {
  switch (key) {
    case "title": return item.title?.toLowerCase() ?? null;
    case "vendors": return sessionVendors(item).join(", ").toLowerCase();
    case "updated": return item.updated_at ?? item.started_at ?? null;
    default: return null;
  }
}

export function SessionsRoute() {
  const [filter, setFilter] = React.useState("");
  const deferredFilter = React.useDeferredValue(filter);
  const [page, setPage] = React.useState(0);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: fetchSessions });
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });
  const getKey = React.useCallback(getSortValue, []);

  const filtered = React.useMemo(() => {
    const term = deferredFilter.trim().toLowerCase();
    const items = sessions.data?.items ?? [];
    if (!term) return items;
    return items.filter((item) =>
      `${sessionId(item)} ${item.title ?? ""} ${sessionVendors(item).join(" ")} ${item.project_name ?? ""}`.toLowerCase().includes(term)
    );
  }, [deferredFilter, sessions.data?.items]);

  const { sort, handleSort, sorted } = useSort(filtered, getKey);
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const rows = sorted.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  React.useEffect(() => { setPage(0); }, [deferredFilter]);

  const cleanupCount = overview.data?.cleanup.sessions.candidate_count;

  return (
    <div className="route-stack">
      <RouteHeader eyebrow="Session stream" title="Recent session entry points, kept compact for triage." action={<RefreshButton queries={["sessions"]} />} />
      <section className="operations-bar">
        <div className="operation-entry">
          <div className="operation-info">
            <Trash2 size={18} />
            <div>
              <p className="operation-name">Cleanup</p>
              <p className="muted">
                {cleanupCount != null ? `${cleanupCount} candidate(s) ready for review` : "Loading cleanup info\u2026"}
              </p>
            </div>
          </div>
          <Link to="/cleanup">
            <Button size="sm" variant="secondary">Open</Button>
          </Link>
        </div>
      </section>
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, vendor, project, or id" />
      {sessions.isPending ? <TableSkeleton rows={6} cols={4} /> : null}
      {sessions.isError ? <StateBlock title="Session scan failed" detail={sessions.error.message} /> : null}
      {sessions.data ? (
        <>
          <div className="table-shell">
            <Table>
              <TableHead>
                <TableRow>
                  <TableHeader>Session</TableHeader>
                  <TableHeader><SortableHeader label="Vendors" sortKey="vendors" currentSort={sort} onSort={handleSort} /></TableHeader>
                  <TableHeader><SortableHeader label="Title" sortKey="title" currentSort={sort} onSort={handleSort} /></TableHeader>
                  <TableHeader><SortableHeader label="Updated" sortKey="updated" currentSort={sort} onSort={handleSort} /></TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((row, index) => (
                  <TableRow key={`${sessionId(row) ?? index}-${row.updated_at ?? ""}`}>
                    <TableCell className="mono-cell">{shortId(sessionId(row))}</TableCell>
                    <TableCell><VendorBadges vendors={sessionVendors(row)} /></TableCell>
                    <TableCell>{row.title ?? "-"}</TableCell>
                    <TableCell className="path-cell" title={row.updated_at ?? row.started_at ?? ""}>{relativeTime(row.updated_at ?? row.started_at)}</TableCell>
                  </TableRow>
                ))}
                {!rows.length ? (
                  <TableRow><TableCell colSpan={4}>No sessions match the current filter.</TableCell></TableRow>
                ) : null}
              </TableBody>
            </Table>
          </div>
          <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}
    </div>
  );
}
