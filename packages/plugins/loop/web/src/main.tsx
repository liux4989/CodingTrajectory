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
  const inventory = useCore<ProjectListResponse>("project.list", {});
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
                {Object.entries(inventory.data?.result.items ?? {}).map(
                  ([name, details]) => (
                    <SidebarMenuItem key={name}>
                      <SidebarMenuButton
                        title={details.path ?? details.display_name}
                        isActive={!reference && project === name}
                        aria-current={
                          !reference && project === name ? "page" : undefined
                        }
                        onClick={() => {
                          setProject(name);
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
            Local evidence · latest
          </div>
        </header>
        {monitorRoute ? (
          <MonitorHome route={monitorRoute} />
        ) : reference ? (
          <InvestigationView
            key={`${reference.session_id}:${savedView?.id ?? ""}`}
            reference={reference}
            onSave={loadSaved}
            savedView={savedView}
          />
        ) : (
          <Explore
            key={project}
            project={project}
            projectName={
              inventory.data?.result.items[project]?.display_name ?? ""
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
  const sessions = useCore<ProjectSessionsResponse>(
    "project.sessions",
    project ? { project_id: project } : {},
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
        Core inventory is unpaginated. Coverage is inspected when you open a
        session.
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
                  href={referenceLink({ session_id: session.root_session_id })}
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
                      <EvidenceLink reference={{ session_id: id }}>
                        Session {short(id)}
                      </EvidenceLink>
                    </p>
                  ))}
                </details>
              )}
            </div>
            <Button asChild variant="outline">
              <a href={referenceLink({ session_id: session.root_session_id })}>
                Investigate
                <ArrowUpRight data-icon="inline-end" />
              </a>
            </Button>
          </article>
        ))}
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
  const summary = useCore<SessionSummaryResponse>("session.summary", {
    session_id: reference.session_id,
    ...(reference.turn_id ? { turn_id: reference.turn_id } : {}),
  });
  const overview = useCore<SessionOverviewResponse>("session.overview", {
    session_id: reference.session_id,
    limit: 5,
    ...(turnCursors.length
      ? { before_turn_id: turnCursors.at(-1) }
      : {}),
  });
  const brief = summary.data?.result;
  const overviewTurns = (overview.data?.result.sessions ?? []).flatMap(
    (session) => rows(session.turns),
  );
  const oldestTurnId = string(overviewTurns.at(-1)?.turn_id);
  async function save() {
    setSaveState("Saving…");
    try {
      await request("/api/investigations", {
        id,
        title,
        reference,
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
          <Badge variant="outline">Latest local evidence · not pinned</Badge>
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
                  <EvidenceLink reference={cite(brief.objective.references)}>
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
                        <EvidenceLink reference={cite(claim.references)}>
                          {claim.text}
                        </EvidenceLink>
                      </p>
                    ))}
                  </div>
                ))}
                {!!brief.changes?.length && <h3>Changes</h3>}
                {brief.changes?.map((change, i) => (
                  <p key={i}>
                    <EvidenceLink reference={cite(change.references)}>
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
                        <EvidenceLink reference={cite(entry.references)}>
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
            {overview.loading && <Loading />}
            {overview.data?.result.sessions.map((session, i) => (
              <div key={string(session.session_id) || i}>
                {rows(session.turns).map((turn, j) => (
                  <article className="turn" key={string(turn.turn_id) || j}>
                    <div className="section-heading">
                      <h3>Turn {Number(turn.sequence) + 1}</h3>
                      <Badge variant="outline">
                        {string(turn.status) || "Unknown status"}
                      </Badge>
                    </div>
                    <p className="muted small">{date(turn.started_at)}</p>
                    <p>
                      {string(object(turn.user_request).content) ||
                        "Request content unavailable"}
                    </p>
                    {rows(turn.activity).map((activity, index) => (
                      <div className="activity" key={index}>
                        <p>
                          {string(activity.text) ||
                            string(activity.label) ||
                            "Activity"}
                        </p>
                        {Array.isArray(activity.item_ids) &&
                          activity.item_ids
                            .filter((v): v is string => typeof v === "string")
                            .map((item) => (
                              <EvidenceLink
                                key={item}
                                reference={{
                                  session_id: string(session.session_id),
                                  turn_id: string(turn.turn_id),
                                  item_id: item,
                                }}
                              >
                                Item {short(item)}
                              </EvidenceLink>
                            ))}
                      </div>
                    ))}
                    <EvidenceLink
                      reference={{
                        session_id: string(session.session_id),
                        turn_id: string(turn.turn_id),
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
                {overviewTurns.length >= 5 && oldestTurnId && (
                  <Button
                    variant="outline"
                    onClick={() =>
                      setTurnCursors((cursors) => [...cursors, oldestTurnId])
                    }
                  >
                    Older turns
                  </Button>
                )}
                {!overview.data.result.sessions.some(
                  (session) => rows(session.turns).length,
                ) && <p className="muted small">No turns in this window.</p>}
              </div>
            )}
          </section>
          <Items
            key={`${reference.session_id}:${reference.turn_id}`}
            reference={reference}
          />
        </div>
        <Evidence reference={reference} />
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
        Read canonical metadata first. Open an item to fetch its local content.
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
        <Button variant="outline" disabled={loading} onClick={load}>
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
      ...(reference.turn_id ? { turn_id: reference.turn_id } : {}),
      ...(isEvent
        ? { event_ids: [reference.event_id], limit: 1 }
        : { item_ids: [reference.item_id], limit: 1 }),
    },
    selected,
  );
  const item = isEvent ? undefined : detail.data?.result.items?.[0];
  const event = isEvent ? detail.data?.result.events?.[0] : undefined;
  const candidate = item ?? event;
  const record =
    candidate &&
    Object.entries(reference).every(
      ([key, value]) => !value || candidate[key] === value,
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
        No revision pinning. A link opens the latest available local evidence,
        not an archived snapshot.
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
