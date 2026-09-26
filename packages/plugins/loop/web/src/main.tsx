import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowLeft,
  ArrowUpRight,
  Bookmark,
  Copy,
  Flag,
  Folder,
  Infinity,
  Radar,
  Search,
  Terminal,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import type { ProjectListResponse } from "./generated/project.list";
import type { ProjectSessionsResponse } from "./generated/project.sessions";
import type { SessionSummaryResponse } from "./generated/session.summary";
import type { SessionOverviewResponse } from "./generated/session.overview";
import type {
  SessionItemsResponse,
  CanonicalItemRecord,
} from "./generated/session.items";
import type { SessionEventsResponse } from "./generated/session.events";
import type {
  Investigation,
  CanonicalReference,
} from "./generated/investigation";
import {
  Blank,
  ErrorNotice,
  EvidenceLink,
  Json,
  Loading,
  cite,
  date,
  short,
} from "./shared";
import {
  useCore,
  request,
  referenceLink,
  readReference,
  object,
  rows,
  string,
  type CoreResult,
} from "./api";
import { readMonitorRoute } from "./monitor-api";
import { MonitorHome } from "./monitor";
import { ToolMix } from "./tool-mix";
import "./styles.css";

const monitorNavigation = [
  ["strategies", "Strategies", Radar],
  ["activity", "Activity", Activity],
  ["findings", "Findings", Flag],
] as const;

function App() {
  const [reference, setReference] = useState(readReference);
  const [monitorRoute, setMonitorRoute] = useState(readMonitorRoute);
  const [project, setProject] = useState("");
  const { setOpenMobile, openMobile, open, isMobile } = useSidebar();
  const [saved, setSaved] = useState<Investigation[]>([]);
  const [savedError, setSavedError] = useState<string>();
  const [projectCursors, setProjectCursors] = useState<string[]>([]);
  const inventory = useCore<ProjectListResponse>("project.list", { cursor: projectCursors.at(-1) });
  const savedView = saved.find(
    (view) =>
      view.id ===
      new URLSearchParams(location.hash.slice(1)).get("investigation_id"),
  );
  useEffect(() => {
    const change = () => {
      setReference(readReference());
      setMonitorRoute(readMonitorRoute());
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  function loadSaved() {
    request<{ items: Investigation[] }>("/api/investigations")
      .then((data) => {
        setSaved(data.items);
        setSavedError(undefined);
      })
      .catch((error) => setSavedError(error.message));
  }
  useEffect(loadSaved, []);
  return (
    <>
      <a
        href="#main"
        className="skip-link"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main")?.focus();
        }}
      >
        Skip to content
      </a>
      <Sidebar>
        <SidebarHeader className="px-4 py-5 pr-14">
          <a href="#" className="brand" onClick={() => setOpenMobile(false)}>
            <Infinity aria-hidden="true" />
            <strong>Loop</strong>
          </a>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton asChild isActive={!reference}>
                  <a
                    href="#"
                    aria-current={!reference ? "page" : undefined}
                    onClick={() => setOpenMobile(false)}
                  >
                    <Search aria-hidden="true" /> Explore
                  </a>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel asChild>
              <h2>Monitor</h2>
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {monitorNavigation.map(([view, label, Icon]) => (
                  <SidebarMenuItem key={String(view)}>
                    <SidebarMenuButton
                      asChild
                      isActive={monitorRoute?.view === view}
                    >
                      <a
                        href={`#/monitor/${view}`}
                        aria-current={
                          monitorRoute?.view === view ? "page" : undefined
                        }
                        onClick={() => setOpenMobile(false)}
                      >
                        <Icon aria-hidden="true" />
                        <span>{String(label)}</span>
                      </a>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel asChild>
              <h2>Projects</h2>
            </SidebarGroupLabel>
            <SidebarGroupContent>
              {inventory.loading && <Loading />}
              <ErrorNotice message={inventory.error} />
              <SidebarMenu>
                <SidebarMenuItem>
                  <SidebarMenuButton
                    isActive={!reference && !project}
                    aria-current={!reference && !project ? "page" : undefined}
                    onClick={() => {
                      setProject("");
                      location.hash = "";
                      setOpenMobile(false);
                    }}
                  >
                    All local sessions
                  </SidebarMenuButton>
                </SidebarMenuItem>
                {(inventory.data?.result.items ?? []).map(
                  (details) => (
                    <SidebarMenuItem key={details.project_id}>
                      <SidebarMenuButton
                        title={details.path ?? details.display_name}
                        isActive={!reference && project === details.project_id}
                        aria-current={
                          !reference && project === details.project_id ? "page" : undefined
                        }
                        onClick={() => {
                          setProject(details.project_id);
                          location.hash = "";
                          setOpenMobile(false);
                        }}
                      >
                        <Folder aria-hidden="true" />
                        <span>{details.display_name}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ),
                )}
              </SidebarMenu>
              <div className="flex gap-2">
                {!!projectCursors.length && <Button variant="outline" onClick={() => setProjectCursors(value => value.slice(0, -1))}>Previous projects</Button>}
                {inventory.data?.result.next_cursor && <Button variant="outline" onClick={() => setProjectCursors(value => [...value, inventory.data!.result.next_cursor!])}>More projects</Button>}
              </div>
            </SidebarGroupContent>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel asChild>
              <h2>Saved investigations</h2>
            </SidebarGroupLabel>
            <SidebarGroupContent>
              {savedError && <ErrorNotice message={savedError} />}
              <nav aria-label="Saved investigations">
                <SidebarMenu>
                  {saved.map((view) => (
                    <SidebarMenuItem key={view.id}>
                      <SidebarMenuButton
                        asChild
                        isActive={savedView?.id === view.id}
                      >
                        <a
                          title={view.title}
                          aria-current={
                            savedView?.id === view.id ? "page" : undefined
                          }
                          href={`${referenceLink(view.reference)}&investigation_id=${view.id}`}
                          onClick={() => setOpenMobile(false)}
                        >
                          <Bookmark aria-hidden="true" />
                          <span>{view.title}</span>
                        </a>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </nav>
              {!saved.length && (
                <p className="muted small px-2 py-2">
                  Save a reading position from an investigation.
                </p>
              )}
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter className="p-4">
          <p className="muted small">
            Evidence stays on this host.
            <br />
            Links identify records; they do not grant access.
          </p>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset id="main" tabIndex={-1}>
        <header className="app-header">
          <SidebarTrigger
            id="navigation-trigger"
            aria-label="Toggle navigation"
            aria-expanded={isMobile ? openMobile : open}
            className="size-11 shrink-0"
          />
          <strong className="md:hidden">Loop</strong>
          <span className="product-name">CodingTrajectory</span>
          <div className="source-status">
            <span />
            Local evidence
          </div>
        </header>
        {monitorRoute ? (
          <MonitorHome route={monitorRoute} />
        ) : reference ? (
          <InvestigationView
            key={`${reference.session_id}:${reference.view_manifest_sha256 ?? ""}:${savedView?.id ?? ""}`}
            reference={reference}
            onSave={loadSaved}
            savedView={savedView}
          />
        ) : (
          <Explore
            key={project}
            project={project}
            projectName={
              inventory.data?.result.items.find(item => item.project_id === project)?.display_name ?? ""
            }
          />
        )}
      </SidebarInset>
    </>
  );
}

function Explore({
  project,
  projectName,
}: {
  project: string;
  projectName: string;
}) {
  const [filter, setFilter] = useState("");
  const [cursors, setCursors] = useState<string[]>([]);
  const sessions = useCore<ProjectSessionsResponse>(
    "project.sessions",
    { ...(project ? { project_id: project } : {}), cursor: cursors.at(-1) },
  );
  const all = sessions.data?.result.items ?? [];
  const matches = all.filter((session) =>
    [
      session.title,
      session.project,
      session.root_session_id,
      ...(session.vendors ?? []),
    ]
      .join(" ")
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  return (
    <section className="explore">
      <h1>Explore</h1>
      <p className="lede">Follow a coding session back to its evidence.</p>
      <div className="explore-toolbar">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="filter">Filter discovered sessions</FieldLabel>
            <Input
              id="filter"
              placeholder="Project, session ID, title, or vendor"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </Field>
        </FieldGroup>
      </div>
      <div className="section-heading">
        <h2>{projectName || "Local sessions"}</h2>
        <span className="muted small">
          {matches.length} of {all.length} discovered
        </span>
      </div>
      <p className="muted small">
        This page contains {all.length} of {sessions.data?.result.total ?? "…"} sessions. The metadata filter applies to this page.
      </p>
      <ErrorNotice message={sessions.error} />
      {sessions.loading && <Loading />}
      <div className="session-list">
        {matches.map((session) => (
          <article className="session-row" key={session.root_session_id}>
            <div>
              <div className="flex flex-wrap gap-2">
                <Badge variant="secondary">
                  {session.project ?? "Project unavailable"}
                </Badge>
                {session.vendors?.map((vendor) => (
                  <Badge key={vendor} variant="outline">
                    {vendor}
                  </Badge>
                ))}
              </div>
              <h3>
                <a
                  href={referenceLink({ session_id: session.root_session_id, view_manifest_sha256: session.view_manifest_sha256 })}
                >
                  {session.title || `Session ${short(session.root_session_id)}`}
                  <ArrowUpRight size={18} />
                </a>
              </h3>
              {session.preview && <p>{session.preview}</p>}
              <p className="muted small">
                <code>{session.root_session_id}</code> ·{" "}
                {session.session_ids?.length ?? 1} observed member(s)
              </p>
              {session.warnings?.map((warning) => (
                <p key={warning}>{warning}</p>
              ))}
              {(session.session_ids?.length ?? 0) > 1 && (
                <details>
                  <summary>Open a member session</summary>
                  {session.session_ids?.map((id) => (
                    <p key={id}>
                      <EvidenceLink reference={{ session_id: id, view_manifest_sha256: session.view_manifest_sha256 }}>
                        Session {short(id)}
                      </EvidenceLink>
                    </p>
                  ))}
                </details>
              )}
            </div>
            <Button asChild variant="outline">
              <a href={referenceLink({ session_id: session.root_session_id, view_manifest_sha256: session.view_manifest_sha256 })}>
                Investigate
                <ArrowUpRight data-icon="inline-end" />
              </a>
            </Button>
          </article>
        ))}
      </div>
      <div className="flex gap-2">
        {!!cursors.length && <Button variant="outline" onClick={() => setCursors(value => value.slice(0, -1))}>Previous sessions</Button>}
        {sessions.data?.result.next_cursor && <Button variant="outline" onClick={() => setCursors(value => [...value, sessions.data!.result.next_cursor!])}>More sessions</Button>}
      </div>
      {!sessions.loading && !sessions.error && !matches.length && (
        <Blank
          title={
            all.length ? "No matching sessions" : "No local sessions found"
          }
        >
          {all.length
            ? "Change the metadata filter to see other sessions."
            : "Capture logs with a supported coding agent on this host, then reload. Loop never falls back to a hosted source."}
        </Blank>
      )}
    </section>
  );
}

function InvestigationView({
  reference,
  onSave,
  savedView,
}: {
  reference: CanonicalReference;
  onSave: () => void;
  savedView?: Investigation;
}) {
  const [id] = useState(() => savedView?.id ?? crypto.randomUUID());
  const [saveState, setSaveState] = useState("");
  useEffect(() => setSaveState(""), [reference]);
  const [title, setTitle] = useState(
    savedView?.title ?? `Session ${short(reference.session_id)}`,
  );
  const [turnCursors, setTurnCursors] = useState<string[]>([]);
  const [viewHash, setViewHash] = useState(reference.view_manifest_sha256);
  const overview = useCore<SessionOverviewResponse>("session.overview", {
    session_id: reference.session_id,
    view_manifest_sha256: viewHash,
    limit: 5,
    ...(turnCursors.length
      ? { cursor: turnCursors.at(-1) }
      : {}),
  });
  useEffect(() => {
    if (overview.data?.meta?.identity?.view_manifest_sha256) setViewHash(overview.data.meta.identity.view_manifest_sha256);
  }, [overview.data]);
  const pinnedReference = { ...reference, view_manifest_sha256: viewHash };
  const summary = useCore<SessionSummaryResponse>("session.summary", {
    session_id: reference.session_id,
    view_manifest_sha256: viewHash,
    ...(reference.turn_id ? { turn_id: reference.turn_id } : {}),
  }, Boolean(viewHash));
  const brief = summary.data?.result;
  const overviewTurns = overview.data?.result.turns ?? [];
  const nextTurnCursor = overview.data?.result.page.next_cursor;
  async function save() {
    setSaveState("Saving…");
    try {
      await request("/api/investigations", {
        id,
        title,
        reference: pinnedReference,
        source: "host_local",
        revision: "latest",
      });
      setSaveState("Saved on this host");
      onSave();
    } catch (error) {
      setSaveState(String(error));
    }
  }
  return (
    <div className="investigation">
      <div className="investigation-header">
        <a className="back-link" href="#">
          <ArrowLeft size={15} />
          Explore
        </a>
        <div className="section-heading">
          <h1>Investigation</h1>
          <Badge variant="outline">{viewHash ? "Pinned prepared evidence" : "Loading evidence identity"}</Badge>
        </div>
        <code className="session-id">{reference.session_id}</code>
        <div className="save-toolbar">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="view-title" className="sr-only">
                Investigation title
              </FieldLabel>
              <Input
                id="view-title"
                maxLength={160}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </Field>
          </FieldGroup>
          <Button
            variant="outline"
            disabled={!title.trim() || saveState === "Saving…"}
            onClick={save}
          >
            <Bookmark data-icon="inline-start" />
            Save view
          </Button>
          <span className="small" role="status">
            {saveState}
          </span>
        </div>
        {(reference.item_id || reference.event_id) && (
          <Button
            className="reading-jump"
            variant="outline"
            onClick={() => {
              document.getElementById("exact-evidence")?.focus();
              document
                .getElementById("exact-evidence")
                ?.scrollIntoView({ block: "start" });
            }}
          >
            Go to exact evidence
            <ArrowUpRight data-icon="inline-end" />
          </Button>
        )}
      </div>
      <div className="reading-layout">
        <div className="chronology">
          <section className="brief" id="session-brief" tabIndex={-1}>
            <h2>Session brief</h2>
            <ErrorNotice message={summary.error} />
            {summary.loading && <Loading />}
            {brief && (
              <>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="secondary">
                    {brief.latest_turn_status ?? "Status unavailable"}
                  </Badge>
                  <Badge variant="outline">{brief.coverage.retention}</Badge>
                  <Badge variant="outline">
                    {brief.coverage.measurement} measurements
                  </Badge>
                </div>
                <p className="objective">
                  {brief.objective?.text ??
                    "No objective retained in this scope."}
                </p>
                {brief.objective && (
                  <EvidenceLink reference={{ ...cite(brief.objective.references), view_manifest_sha256: viewHash }}>
                    Inspect objective evidence
                  </EvidenceLink>
                )}
                {brief.warnings?.map((warning) => (
                  <p className="coverage-note" key={warning}>
                    {warning}
                  </p>
                ))}
                {(
                  [
                    ["Decisions", brief.decisions ?? []],
                    ["Next actions", brief.next_actions ?? []],
                  ] as const
                ).map(([label, values]) => (
                  <div key={String(label)}>
                    {values.length > 0 && <h3>{String(label)}</h3>}
                    {values.map((claim, i) => (
                      <p key={i}>
                        <EvidenceLink reference={{ ...cite(claim.references), view_manifest_sha256: viewHash }}>
                          {claim.text}
                        </EvidenceLink>
                      </p>
                    ))}
                  </div>
                ))}
                {!!brief.changes?.length && <h3>Changes</h3>}
                {brief.changes?.map((change, i) => (
                  <p key={i}>
                    <EvidenceLink reference={{ ...cite(change.references), view_manifest_sha256: viewHash }}>
                      {change.path} · {change.operations?.join(", ")}
                    </EvidenceLink>
                  </p>
                ))}
                {(
                  [
                    ["Verification", brief.verification ?? []],
                    ["Unresolved", brief.unresolved ?? []],
                  ] as const
                ).map(([label, values]) => (
                  <div key={String(label)}>
                    {values.length > 0 && <h3>{String(label)}</h3>}
                    {values.map((entry, i) => (
                      <p key={i}>
                        <EvidenceLink reference={{ ...cite(entry.references), view_manifest_sha256: viewHash }}>
                          {entry.label} · {entry.status}
                        </EvidenceLink>
                      </p>
                    ))}
                  </div>
                ))}
                <p className="muted small">
                  Projection: {brief.projection.name} v
                  {brief.projection.version} · {brief.projection.strategy}
                </p>
                {Object.entries(brief.truncation ?? {})
                  .filter(([, status]) => status.truncated)
                  .map(([label, status]) => (
                    <p className="small" key={label}>
                      {label}: bounded view of {status.total} entries
                    </p>
                  ))}
              </>
            )}
          </section>
          <ToolMix
            reference={pinnedReference}
            enabled={Boolean(viewHash) && !summary.loading}
          />
          <section>
            <div className="section-heading">
              <h2>Chronology</h2>
              <span className="small muted">
                Up to 5 turns ·{" "}
                {turnCursors.length
                  ? `history page ${turnCursors.length + 1}`
                  : "most recent"}
              </span>
            </div>
            <ErrorNotice message={overview.error} />
            {overview.error && <a href={referenceLink({ session_id: reference.session_id })} onClick={() => { setTurnCursors([]); setViewHash(undefined); }}>Open latest available view</a>}
            {overview.loading && <Loading />}
            {overview.data?.result.sessions.map((session, i) => (
              <div key={string(session.session_id) || i}>
                <p className="small">{session.cwd && <>cwd: <code className="break-all">{session.cwd}</code><br /></>}{session.agent_path && <>agent_path: <code className="break-all">{session.agent_path}</code></>}</p>
                {overviewTurns.filter(turn => turn.session_id === session.session_id).map((turn, j) => (
                  <article className="turn" key={string(turn.turn_id) || j}>
                    <div className="section-heading">
                      <h3>Turn {turn.source_turn_ordinal + 1}</h3>
                      <Badge variant="outline">
                        {string(turn.status) || "Unknown status"}
                      </Badge>
                    </div>
                    <p className="muted small">{date(turn.started_at)}</p>
                    <p>
                      {string(object(turn.user_request).content) ||
                        "Request content unavailable"}
                    </p>
                    {turn.assistant_responses.map(response => <p key={response.item_id}>{response.preview}</p>)}
                    {turn.activities.map((activity, index) => (
                      <div className="activity" key={index}>
                        <p>
                          {activity.tool}
                          {(activity.count ?? 1) > 1 ? ` ×${activity.count}` : ""} ·{" "}
                          {activity.target || activity.path || activity.cmd || activity.query || activity.url || activity.task || "Target not retained"} ·{" "}
                          {activity.outcome || activity.status || "Outcome unknown"}
                        </p>
                        {activity.item_ids.map((item_id) => (
                          <EvidenceLink
                            key={item_id}
                            reference={{
                              session_id: string(session.session_id),
                              turn_id: string(turn.turn_id),
                              item_id,
                              view_manifest_sha256: viewHash,
                            }}
                          >
                            Item {short(item_id)}
                          </EvidenceLink>
                        ))}
                      </div>
                    ))}
                    <EvidenceLink
                      reference={{
                        session_id: string(session.session_id),
                        turn_id: string(turn.turn_id),
                        view_manifest_sha256: viewHash,
                      }}
                    >
                      Inspect turn items
                    </EvidenceLink>
                  </article>
                ))}
              </div>
            ))}
            {overview.data && (
              <div className="flex gap-2">
                {turnCursors.length > 0 && (
                  <Button
                    variant="outline"
                    onClick={() => setTurnCursors((cursors) => cursors.slice(0, -1))}
                  >
                    Newer turns
                  </Button>
                )}
                {nextTurnCursor && (
                  <Button
                    variant="outline"
                    onClick={() =>
                      setTurnCursors((cursors) => [...cursors, nextTurnCursor])
                    }
                  >
                    Older turns
                  </Button>
                )}
                {!overviewTurns.length && <p className="muted small">No turns in this window.</p>}
              </div>
            )}
          </section>
          <Items
            key={`${reference.session_id}:${reference.turn_id}:${viewHash}`}
            reference={pinnedReference}
          />
        </div>
        <Evidence reference={pinnedReference} />
      </div>
    </div>
  );
}

function Items({ reference }: { reference: CanonicalReference }) {
  const [items, setItems] = useState<CanonicalItemRecord[]>([]);
  const [cursor, setCursor] = useState<string | null | undefined>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  async function load() {
    setLoading(true);
    setError(undefined);
    try {
      const data = await request<CoreResult<SessionItemsResponse>>(
        "/api/core",
        {
          method: "session.items",
          params: {
            session_id: reference.session_id,
            view_manifest_sha256: reference.view_manifest_sha256,
            ...(reference.turn_id ? { turn_id: reference.turn_id } : {}),
            limit: 20,
            ...(cursor ? { cursor } : {}),
          },
        },
      );
      setItems((previous) => [...previous, ...(data.result.items ?? [])]);
      setCursor(data.result.next_cursor ?? null);
    } catch (error) {
      setError(String(error));
    } finally {
      setLoading(false);
    }
  }
  return (
    <section className="items">
      <div className="section-heading">
        <h2>{reference.turn_id ? "Turn items" : "Session items"}</h2>
        <span className="small muted">
          {items.length} loaded · source order
        </span>
      </div>
      <p className="small muted">
        Read canonical metadata first. Open an item to inspect its retained evidence.
      </p>
      <ErrorNotice message={error} />
      {items.map((item) => (
        <article className="item-row" key={item.item_id}>
          <div className="flex flex-wrap gap-2">
            <Badge variant="secondary">{item.kind}</Badge>
            <Badge variant="outline">{item.status ?? "Status unknown"}</Badge>
          </div>
          <EvidenceLink
            reference={{
              session_id: item.session_id,
              turn_id: item.turn_id,
              item_id: item.item_id,
              view_manifest_sha256: reference.view_manifest_sha256,
            }}
          >
            {item.operation ?? item.kind} · {short(item.item_id)}
          </EvidenceLink>
          <p className="muted small">
            {item.provenance.source} · {item.coverage.retention} retention
          </p>
        </article>
      ))}
      {cursor !== null && (
        <Button variant="outline" disabled={loading || !reference.view_manifest_sha256} onClick={load}>
          {loading
            ? "Loading items…"
            : cursor
              ? "Load next 20 items"
              : "Load canonical items"}
        </Button>
      )}
      {cursor === null && <p className="muted small">End of scoped items.</p>}
    </section>
  );
}

function Evidence({ reference }: { reference: CanonicalReference }) {
  const isEvent = Boolean(reference.event_id);
  const selected = Boolean(reference.item_id || reference.event_id);
  const detail = useCore<SessionItemsResponse & SessionEventsResponse>(
    isEvent ? "session.events" : "session.items",
    {
      session_id: reference.session_id,
      view_manifest_sha256: reference.view_manifest_sha256,
      ...(reference.turn_id ? { turn_id: reference.turn_id } : {}),
      ...(isEvent
        ? { event_ids: [reference.event_id], limit: 1 }
        : { item_ids: [reference.item_id], limit: 1 }),
    },
    selected && Boolean(reference.view_manifest_sha256),
  );
  const item = isEvent ? undefined : detail.data?.result.items?.[0];
  const event = isEvent ? detail.data?.result.events?.[0] : undefined;
  const candidate = item ?? event;
  const record =
    candidate &&
    Object.entries(reference).every(
      ([key, value]) => key === "view_manifest_sha256" || !value || candidate[key] === value,
    )
      ? candidate
      : undefined;
  const [copied, setCopied] = useState("");
  useEffect(() => setCopied(""), [reference]);
  async function copy(kind: "link" | "reference") {
    try {
      await navigator.clipboard.writeText(
        kind === "link"
          ? new URL(referenceLink(reference), location.href).href
          : JSON.stringify(
              {
                protocol: "ct.loop.v1",
                source: "host_local",
                revision: "latest",
                reference,
              },
              null,
              2,
            ),
      );
      setCopied(kind === "link" ? "Link copied" : "Reference copied");
    } catch {
      setCopied("Clipboard unavailable. Select and copy the reference below.");
    }
  }
  return (
    <aside
      className="evidence-panel"
      id="exact-evidence"
      tabIndex={-1}
      data-selected={selected}
      aria-label="Exact evidence"
    >
      <div className="section-heading">
        <h2>Exact evidence</h2>
        {selected && (
          <Button asChild size="icon" variant="ghost">
            <a
              aria-label="Close evidence"
              href={referenceLink(
                {
                  session_id: reference.session_id,
                  turn_id: reference.turn_id,
                },
                new URLSearchParams(location.hash.slice(1)).get(
                  "investigation_id",
                ),
              )}
            >
              <X />
            </a>
          </Button>
        )}
      </div>
      <p className="muted small">Stable identity, resolved on this host.</p>
      <div className="flex flex-wrap gap-2 py-3">
        <Button size="sm" variant="outline" onClick={() => copy("reference")}>
          <Copy data-icon="inline-start" />
          Copy reference
        </Button>
        <Button size="sm" variant="ghost" onClick={() => copy("link")}>
          Copy link
        </Button>
      </div>
      <span role="status" className="small">
        {copied}
      </span>
      <ErrorNotice message={detail.error} />
      {detail.loading && <Loading />}
      {!selected && (
        <Blank title="Choose a piece of evidence">
          Open an activity or load canonical items. Content is fetched only when
          you request it.
        </Blank>
      )}
      {selected && !detail.loading && !detail.error && !record && (
        <Blank title="Evidence not available">
          This reference did not resolve in the selected local scope. It may be
          missing or no longer retained.
        </Blank>
      )}
      {record && (
        <>
          <div className="evidence-identity">
            <Terminal size={17} />
            <strong>{item?.kind ?? event?.type}</strong>
            <Badge variant="outline">{record.status ?? "Unknown status"}</Badge>
          </div>
          <dl className="provenance">
            {Object.entries({ ...record.provenance, ...record.coverage }).map(
              ([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{String(value)}</dd>
                </div>
              ),
            )}
          </dl>
          <h3>Bounded evidence</h3>
          <Json
            value={
              item
                ? {
                    detail: item.detail,
                    measurements: item.measurements,
                    output_evidence: item.output_evidence,
                    preview: item.preview,
                  }
                : {
                    usage: event?.usage,
                    output_evidence_id: event?.output_evidence_id,
                  }
            }
          />
          {event?.item_id && (
            <p>
              <EvidenceLink
                reference={{
                  session_id: event.session_id,
                  turn_id: event.turn_id ?? undefined,
                  item_id: event.item_id,
                  view_manifest_sha256: reference.view_manifest_sha256,
                }}
              >
                Resolve owning item evidence
              </EvidenceLink>
            </p>
          )}
          {item && (
            <>
              <h3>Underlying events</h3>
              {item.event_ids?.map((event_id) => (
                <p key={event_id}>
                  <EvidenceLink
                    reference={{
                      session_id: item.session_id,
                      turn_id: item.turn_id,
                      item_id: item.item_id,
                      event_id,
                      view_manifest_sha256: reference.view_manifest_sha256,
                    }}
                  >
                    Event {short(event_id)}
                  </EvidenceLink>
                </p>
              ))}
              {!item.event_ids?.length && (
                <p className="muted small">No event references retained.</p>
              )}
            </>
          )}
          <details>
            <summary>Canonical record and source ordering</summary>
            <Json value={record} />
          </details>
        </>
      )}
      <details className="reference-details">
        <summary>Canonical reference</summary>
        <Json
          value={{
            protocol: "ct.loop.v1",
            source: "host_local",
            revision: "latest",
            reference,
          }}
        />
      </details>
      <p className="muted small">
        Links pin this prepared view while it remains available. If it expires,
        open the latest view to restart; raw transcripts are not archived here.
      </p>
      <Button
        className="reading-jump"
        variant="outline"
        onClick={() => {
          document.getElementById("session-brief")?.focus();
          document
            .getElementById("session-brief")
            ?.scrollIntoView({ block: "start" });
        }}
      >
        <ArrowLeft data-icon="inline-start" />
        Back to session brief
      </Button>
    </aside>
  );
}

createRoot(document.getElementById("root")!).render(
  <SidebarProvider>
    <App />
  </SidebarProvider>,
);
