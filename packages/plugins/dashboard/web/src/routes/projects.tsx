import * as React from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchProjects, fetchOverview, type ProjectItem } from "../api";
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
import { Trash2 } from "lucide-react";

const PAGE_SIZE = 20;

function getSortValue(item: ProjectItem, key: string) {
  switch (key) {
    case "name": return item.name.toLowerCase();
    case "path": return item.path?.toLowerCase() ?? null;
    case "vendors": return item.vendors.join(", ").toLowerCase();
    default: return null;
  }
}

export function ProjectsRoute() {
  const [filter, setFilter] = React.useState("");
  const deferredFilter = React.useDeferredValue(filter);
  const [page, setPage] = React.useState(0);
  const projects = useQuery({ queryKey: ["projects"], queryFn: fetchProjects });
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });
  const getKey = React.useCallback(getSortValue, []);

  const filtered = React.useMemo(() => {
    const term = deferredFilter.trim().toLowerCase();
    const items = projects.data?.items ?? [];
    if (!term) return items;
    return items.filter((item) => `${item.name} ${item.path ?? ""} ${item.vendors.join(" ")}`.toLowerCase().includes(term));
  }, [deferredFilter, projects.data?.items]);

  const { sort, handleSort, sorted } = useSort(filtered, getKey);
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const rows = sorted.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  React.useEffect(() => { setPage(0); }, [deferredFilter]);

  const cleanupCount = overview.data?.cleanup.projects.candidate_count;

  return (
    <div className="route-stack">
      <RouteHeader eyebrow="Project inventory" title="Browse discovered project metadata without exposing raw log paths." action={<RefreshButton queries={["projects"]} />} />
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
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter projects by name, vendor, or path" />
      {projects.isPending ? <TableSkeleton rows={6} cols={3} /> : null}
      {projects.isError ? <StateBlock title="Project scan failed" detail={projects.error.message} /> : null}
      {projects.data ? (
        <>
          <div className="table-shell">
            <Table>
              <TableHead>
                <TableRow>
                  <TableHeader><SortableHeader label="Project" sortKey="name" currentSort={sort} onSort={handleSort} /></TableHeader>
                  <TableHeader><SortableHeader label="Vendors" sortKey="vendors" currentSort={sort} onSort={handleSort} /></TableHeader>
                  <TableHeader><SortableHeader label="Path" sortKey="path" currentSort={sort} onSort={handleSort} /></TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={`${row.name}-${row.path}`}>
                    <TableCell className="strong-cell">{row.name}</TableCell>
                    <TableCell><VendorBadges vendors={row.vendors} /></TableCell>
                    <TableCell className="path-cell">{row.path ?? "-"}</TableCell>
                  </TableRow>
                ))}
                {!rows.length ? (
                  <TableRow><TableCell colSpan={3}>No projects match the current filter.</TableCell></TableRow>
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
