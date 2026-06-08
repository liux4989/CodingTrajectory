import * as React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, RouterProvider, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Activity, Boxes, FolderGit2, RefreshCcw, ShieldAlert, Sparkles, Trash2 } from "lucide-react";
import {
  applyCleanup,
  fetchCleanupPreview,
  fetchOverview,
  fetchProjects,
  fetchSessions,
  type CleanupTarget,
  type ProjectItem,
  type SessionItem,
} from "./api";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./components/ui/table";
import { StateBlock } from "./components/state-block";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

const rootRoute = createRootRoute({
  component: AppShell,
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: OverviewRoute,
});

const projectsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/projects",
  component: ProjectsRoute,
});

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  component: SessionsRoute,
});

const cleanupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/cleanup",
  component: CleanupRoute,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([indexRoute, projectsRoute, sessionsRoute, cleanupRoute]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

function AppShell() {
  return (
    <main className="app-shell">
      <aside className="side-rail" aria-label="Dashboard navigation">
        <div className="brand-lockup">
          <div className="brand-mark">CT</div>
          <div>
            <p className="eyebrow">Plugin Web Program</p>
            <h1>CodingTrajectory</h1>
          </div>
        </div>
        <nav className="nav-list">
          <Link to="/" className="nav-link" activeProps={{ className: "nav-link is-active" }}>
            <Sparkles size={18} /> Overview
          </Link>
          <Link to="/projects" className="nav-link" activeProps={{ className: "nav-link is-active" }}>
            <FolderGit2 size={18} /> Projects
          </Link>
          <Link to="/sessions" className="nav-link" activeProps={{ className: "nav-link is-active" }}>
            <Activity size={18} /> Sessions
          </Link>
          <Link to="/cleanup" className="nav-link" activeProps={{ className: "nav-link is-active" }}>
            <Trash2 size={18} /> Cleanup
          </Link>
        </nav>
        <p className="side-note">
          Runs locally through <code>ct plugin dashboard web</code>. Destructive actions stay preview-first.
        </p>
      </aside>
      <section className="content-stage">
        <Outlet />
      </section>
    </main>
  );
}

function OverviewRoute() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });
  if (overview.isPending) return <StateBlock title="Loading dashboard" detail="Collecting project, session, and cleanup signals." />;
  if (overview.isError) return <StateBlock title="Dashboard unavailable" detail={overview.error.message} />;
  const vendorEntries = Object.entries(overview.data.projects.vendors);
  return (
    <div className="route-stack">
      <RouteHeader
        eyebrow="Operational scan"
        title="A compact control room for projects, sessions, and safe cleanup."
        action={<RefreshButton queries={["overview"]} />}
      />
      <section className="metric-grid">
        <MetricCard label="Projects" value={overview.data.projects.count} detail={`${vendorEntries.length} active vendor source(s)`} />
        <MetricCard label="Recent sessions" value={overview.data.sessions.count} detail="Default 30 day window" />
        <MetricCard label="Project cleanup candidates" value={overview.data.cleanup.projects.candidate_count} detail={`${overview.data.cleanup.projects.skipped_count} skipped`} />
        <MetricCard label="Empty session candidates" value={overview.data.cleanup.sessions.candidate_count} detail={`${overview.data.cleanup.sessions.skipped_count} skipped`} />
      </section>
      <section className="split-grid">
        <Card className="panel-surface">
          <CardHeader>
            <CardTitle>Vendor Coverage</CardTitle>
            <CardDescription>Project metadata grouped by agent vendor.</CardDescription>
          </CardHeader>
          <CardContent className="badge-cloud">
            {vendorEntries.length ? (
              vendorEntries.map(([vendor, count]) => (
                <Badge key={vendor}>
                  {vendor} <strong>{count}</strong>
                </Badge>
              ))
            ) : (
              <p className="muted">No vendor metadata found.</p>
            )}
          </CardContent>
        </Card>
        <Card className="panel-surface">
          <CardHeader>
            <CardTitle>Cleanup Posture</CardTitle>
            <CardDescription>Candidate counts are previews. Nothing moves until you confirm a selected action.</CardDescription>
          </CardHeader>
          <CardContent className="reason-list">
            <ReasonSummary title="Project skips" reasons={overview.data.cleanup.projects.skipped_reasons} />
            <ReasonSummary title="Session skips" reasons={overview.data.cleanup.sessions.skipped_reasons} />
          </CardContent>
        </Card>
      </section>
    </div>
  );
}

function ProjectsRoute() {
  const [filter, setFilter] = React.useState("");
  const deferredFilter = React.useDeferredValue(filter);
  const projects = useQuery({ queryKey: ["projects"], queryFn: fetchProjects });
  const rows = React.useMemo(() => {
    const term = deferredFilter.trim().toLowerCase();
    const items = projects.data?.items ?? [];
    if (!term) return items;
    return items.filter((item) => `${item.name} ${item.path ?? ""} ${item.vendors.join(" ")}`.toLowerCase().includes(term));
  }, [deferredFilter, projects.data?.items]);

  return (
    <div className="route-stack">
      <RouteHeader eyebrow="Project inventory" title="Browse discovered project metadata without exposing raw log paths." action={<RefreshButton queries={["projects"]} />} />
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter projects by name, vendor, or path" />
      {projects.isPending ? <StateBlock title="Loading projects" /> : null}
      {projects.isError ? <StateBlock title="Project scan failed" detail={projects.error.message} /> : null}
      {projects.data ? <ProjectsTable rows={rows} /> : null}
    </div>
  );
}

function SessionsRoute() {
  const [filter, setFilter] = React.useState("");
  const deferredFilter = React.useDeferredValue(filter);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: fetchSessions });
  const rows = React.useMemo(() => {
    const term = deferredFilter.trim().toLowerCase();
    const items = sessions.data?.items ?? [];
    if (!term) return items;
    return items.filter((item) => `${sessionId(item)} ${item.title ?? ""} ${sessionVendors(item).join(" ")} ${item.project_name ?? ""}`.toLowerCase().includes(term));
  }, [deferredFilter, sessions.data?.items]);

  return (
    <div className="route-stack">
      <RouteHeader eyebrow="Session stream" title="Recent session entry points, kept compact for triage." action={<RefreshButton queries={["sessions"]} />} />
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, vendor, project, or id" />
      {sessions.isPending ? <StateBlock title="Loading sessions" /> : null}
      {sessions.isError ? <StateBlock title="Session scan failed" detail={sessions.error.message} /> : null}
      {sessions.data ? <SessionsTable rows={rows} /> : null}
    </div>
  );
}

function CleanupRoute() {
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
  const [selected, setSelected] = React.useState<Set<string>>(() => new Set());
  const [action, setAction] = React.useState<"trash" | "delete">("trash");
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
    onSuccess: () => {
      setSelected(new Set());
      void queryClient.invalidateQueries({ queryKey: ["cleanup", kind] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
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
        {preview.isPending ? <StateBlock title="Scanning cleanup candidates" /> : null}
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
              <Button variant={action === "delete" ? "destructive" : "default"} size="sm" disabled={!selected.size || apply.isPending} onClick={() => apply.mutate()}>
                <ShieldAlert size={15} /> Apply to {selected.size}
              </Button>
            </div>
            {apply.isError ? <p className="error-text">{apply.error.message}</p> : null}
            {apply.data ? (
              <p className="success-text">
                Applied {apply.data.action} to {apply.data.summary.target_count} item(s).
                {apply.data.manifest_path ? ` Manifest: ${apply.data.manifest_path}` : ""}
              </p>
            ) : null}
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
    </Card>
  );
}

function ProjectsTable({ rows }: { rows: ProjectItem[] }) {
  return (
    <div className="table-shell">
      <Table>
        <TableHead>
          <TableRow>
            <TableHeader>Project</TableHeader>
            <TableHeader>Vendors</TableHeader>
            <TableHeader>Path</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={`${row.name}-${row.path}`}>
              <TableCell className="strong-cell">{row.name}</TableCell>
              <TableCell>
                <VendorBadges vendors={row.vendors} />
              </TableCell>
              <TableCell className="path-cell">{row.path ?? "-"}</TableCell>
            </TableRow>
          ))}
          {!rows.length ? (
            <TableRow>
              <TableCell colSpan={3}>No projects match the current filter.</TableCell>
            </TableRow>
          ) : null}
        </TableBody>
      </Table>
    </div>
  );
}

function SessionsTable({ rows }: { rows: SessionItem[] }) {
  return (
    <div className="table-shell">
      <Table>
        <TableHead>
          <TableRow>
            <TableHeader>Session</TableHeader>
            <TableHeader>Vendors</TableHeader>
            <TableHeader>Title</TableHeader>
            <TableHeader>Updated</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((row, index) => (
            <TableRow key={`${sessionId(row) ?? index}-${row.updated_at ?? ""}`}>
              <TableCell className="mono-cell">{shortId(sessionId(row))}</TableCell>
              <TableCell>
                <VendorBadges vendors={sessionVendors(row)} />
              </TableCell>
              <TableCell>{row.title ?? "-"}</TableCell>
              <TableCell className="path-cell">{row.updated_at ?? row.started_at ?? "-"}</TableCell>
            </TableRow>
          ))}
          {!rows.length ? (
            <TableRow>
              <TableCell colSpan={4}>No sessions match the current filter.</TableCell>
            </TableRow>
          ) : null}
        </TableBody>
      </Table>
    </div>
  );
}

function RouteHeader({ eyebrow, title, action }: { eyebrow: string; title: string; action?: React.ReactNode }) {
  return (
    <header className="route-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
      </div>
      {action}
    </header>
  );
}

function MetricCard({ label, value, detail }: { label: string; value: number; detail: string }) {
  return (
    <Card className="metric-card">
      <CardContent>
        <p className="metric-label">{label}</p>
        <p className="metric-value">{value.toLocaleString()}</p>
        <p className="metric-detail">{detail}</p>
      </CardContent>
    </Card>
  );
}

function Toolbar({ value, onChange, placeholder }: { value: string; onChange: (value: string) => void; placeholder: string }) {
  return (
    <form className="toolbar" role="search" onSubmit={(event) => event.preventDefault()}>
      <label htmlFor="route-filter">Filter</label>
      <Input id="route-filter" name="filter" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} autoComplete="off" />
    </form>
  );
}

function RefreshButton({ queries }: { queries: string[] }) {
  const client = useQueryClient();
  return (
    <Button variant="secondary" onClick={() => queries.forEach((query) => void client.invalidateQueries({ queryKey: [query] }))}>
      <RefreshCcw size={16} /> Refresh
    </Button>
  );
}

function ReasonSummary({ title, reasons }: { title: string; reasons: Record<string, number> }) {
  const entries = Object.entries(reasons);
  return (
    <div className="reason-summary">
      <h3>{title}</h3>
      {entries.length ? (
        <div className="badge-cloud">
          {entries.map(([reason, count]) => (
            <Badge key={reason} variant="quiet">
              {reason} <strong>{count}</strong>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="muted">No skip reasons.</p>
      )}
    </div>
  );
}

function VendorBadges({ vendors }: { vendors: string[] }) {
  if (!vendors.length) return <span className="muted">-</span>;
  return (
    <div className="badge-cloud">
      {vendors.map((vendor) => (
        <Badge key={vendor} variant="quiet">
          {vendor}
        </Badge>
      ))}
    </div>
  );
}

function ReasonBadges({ reasons }: { reasons: string[] }) {
  return (
    <div className="badge-cloud">
      {reasons.map((reason) => (
        <Badge key={reason} variant="quiet">
          {reason}
        </Badge>
      ))}
    </div>
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

function shortId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function sessionId(item: SessionItem) {
  return item.root_session_id ?? item.id ?? null;
}

function sessionVendors(item: SessionItem) {
  return item.vendors ?? item.v ?? [];
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
