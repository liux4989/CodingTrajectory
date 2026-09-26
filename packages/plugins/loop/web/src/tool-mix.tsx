import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type {
  CanonicalItemRecord,
  SessionItemsResponse,
} from "./generated/session.items";
import type { SessionStatsResponse } from "./generated/session.stats";
import type { CanonicalReference } from "./generated/investigation";
import {
  request,
  referenceLink,
  useCore,
  object,
  rows,
  string,
  type CoreResult,
} from "./api";
import { ErrorNotice, EvidenceLink, Loading } from "./shared";

// Display groups collapse Core activity concepts; Core owns the classification.
// A concept outside these lists (an unmapped provider tool) stays Other.
const GROUPS = [
  ["Read", ["ReadFile"]],
  ["Search", ["SearchText", "ListFiles", "Explore"]],
  ["Edit", ["EditFile"]],
  ["Write", ["WriteFile"]],
  [
    "Command",
    [
      "RunCommand",
      "command_execution",
      "Interacted with background terminal",
      "Waited for background terminal",
    ],
  ],
  ["Web", ["WebFetch", "WebSearch"]],
  [
    "Agents",
    ["SubagentTask", "AgentCollab", "SessionHandoff", "plan_subagent"],
  ],
  ["Other", []],
] as const;
type Group = (typeof GROUPS)[number][0];
// Sequence color families; see styles.css for the palette validation.
const FAMILIES = [
  ["Explore", "Read", ["Read", "Search"]],
  ["Change", "Edit", ["Edit", "Write"]],
  ["Command", "Command", ["Command"]],
  ["Other", "Other", ["Web", "Agents", "Other"]],
] as const;
const GROUP_OF = new Map<string, Group>(
  GROUPS.flatMap(([group, concepts]) =>
    concepts.map((concept) => [concept, group] as const),
  ),
);
const OPEN_KEY = "loop.toolMix.open";
const SEQUENCE_KEY = "loop.toolMix.sequence";
const TOOL_KINDS = ["tool_call", "command_execution", "file_change", "plan"];
const PAGE = 1000;
// Above this many calls, one block per call stops being readable.
const PER_CALL_LIMIT = 300;
const MAX_PAGES = 5;

type Call = {
  item: CanonicalItemRecord;
  concept: string;
  group: Group;
  tokens: number | null;
  durationMs: number | null;
  viaShell: boolean;
  failed: boolean;
};

function toCall(item: CanonicalItemRecord): Call {
  const concept = item.detail?.concept || item.kind;
  const group = GROUP_OF.get(concept) ?? "Other";
  const measurements = item.measurements;
  const tokens =
    measurements &&
    (typeof measurements.input_tokens === "number" ||
      typeof measurements.output_tokens === "number")
      ? (measurements.input_tokens ?? 0) + (measurements.output_tokens ?? 0)
      : null;
  const duration = item.output_evidence?.duration_ms;
  return {
    item,
    concept,
    group,
    tokens,
    durationMs: typeof duration === "number" ? duration : null,
    viaShell: item.kind === "command_execution" && group !== "Command",
    failed:
      item.status === "failed" || item.output_evidence?.outcome === "failed",
  };
}

const number = (value: number) => new Intl.NumberFormat("en-US").format(value);
const tokens = (value: number) =>
  value >= 10_000 ? `${(value / 1000).toFixed(1)}K` : number(value);
const seconds = (ms: number) =>
  ms >= 60_000
    ? `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
    : `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
const sum = (values: (number | null)[]) =>
  values.reduce<number>((total, value) => total + (value ?? 0), 0);
const label = (call: Call) =>
  call.item.detail?.target ||
  call.item.detail?.path ||
  call.item.detail?.tool_name ||
  call.item.kind;

function readOpen() {
  try {
    return localStorage.getItem(OPEN_KEY) !== "false";
  } catch {
    return true;
  }
}

function collapsedSummary(calls: Call[]) {
  const leaders = GROUPS.map(
    ([group]) =>
      [group, calls.filter((call) => call.group === group).length] as const,
  )
    .filter(([, count]) => count)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(
      ([group, count]) =>
        `${group} ${Math.round((count / calls.length) * 100)}%`,
    );
  return `${number(calls.length)} tool calls · ${leaders.join(" · ")}`;
}

function useToolCalls(reference: CanonicalReference, enabled: boolean) {
  const [state, setState] = useState<{
    calls?: Call[];
    truncated?: boolean;
    error?: string;
  }>({});
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({});
    (async () => {
      const items: CanonicalItemRecord[] = [];
      let cursor: string | null | undefined;
      let pages = 0;
      do {
        const data = await request<CoreResult<SessionItemsResponse>>(
          "/api/core",
          {
            method: "session.items",
            params: {
              session_id: reference.session_id,
              view_manifest_sha256: reference.view_manifest_sha256,
              types: TOOL_KINDS,
              limit: PAGE,
              ...(cursor ? { cursor } : {}),
            },
          },
          controller.signal,
        );
        items.push(...(data.result.items ?? []));
        cursor = data.result.next_cursor;
        pages++;
      } while (cursor && pages < MAX_PAGES);
      if (!controller.signal.aborted)
        setState({ calls: items.map(toCall), truncated: Boolean(cursor) });
    })().catch((error) => {
      if (!controller.signal.aborted)
        setState({ error: String(error.message) });
    });
    return () => controller.abort();
  }, [reference.session_id, reference.view_manifest_sha256, enabled]);
  return state;
}

export function ToolMix({
  reference,
  enabled,
}: {
  reference: CanonicalReference;
  enabled: boolean;
}) {
  // The panel sits above the chronology, so readers can collapse it; the choice
  // is remembered per browser. Loading starts only once the brief has loaded
  // and the panel is open, then stays loaded while the reader selects evidence.
  const [open, setOpen] = useState(readOpen);
  const [started, setStarted] = useState(enabled && open);
  useEffect(() => {
    if (enabled && open) setStarted(true);
  }, [enabled, open]);
  function toggle() {
    setOpen(!open);
    try {
      localStorage.setItem(OPEN_KEY, String(!open));
    } catch {
      // Storage may be unavailable; the panel still toggles for this view.
    }
  }
  const { calls, truncated, error } = useToolCalls(reference, started);
  const stats = useCore<SessionStatsResponse>(
    "session.stats",
    {
      session_id: reference.session_id,
      view_manifest_sha256: reference.view_manifest_sha256,
    },
    started,
  );
  const [focus, setFocus] = useState<Group | null>(null);
  const link = (call: Call): CanonicalReference => ({
    session_id: call.item.session_id,
    turn_id: call.item.turn_id,
    item_id: call.item.item_id,
    view_manifest_sha256: reference.view_manifest_sha256,
  });
  const focused = (calls ?? []).filter(
    (call) => !focus || call.group === focus,
  );
  const topTokens = focused
    .filter((call) => call.tokens)
    .sort((a, b) => b.tokens! - a.tokens!)
    .slice(0, 5);
  const topDuration = focused
    .filter((call) => call.durationMs)
    .sort((a, b) => b.durationMs! - a.durationMs!)
    .slice(0, 5);
  const totalTokens = sum((calls ?? []).map((call) => call.tokens));
  return (
    <section className="tool-mix" aria-labelledby="tool-mix-title">
      <div className="section-heading">
        <h2 id="tool-mix-title">Tool mix &amp; context</h2>
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={open}
          aria-controls="tool-mix-body"
          onClick={toggle}
        >
          {open ? "Hide" : "Show"}
          <ChevronDown
            data-icon="inline-end"
            className={open ? "rotate-180" : undefined}
          />
        </Button>
      </div>
      {!open && (
        <p className="small muted">
          {calls
            ? collapsedSummary(calls)
            : "Hidden. Show to load this session's tool mix."}
        </p>
      )}
      <div id="tool-mix-body" hidden={!open}>
        <p className="small muted">
          Whole session
          {reference.turn_id ? " (not limited to the selected turn)" : ""}.
        </p>
        <p className="small muted">
          Grouped by Core activity concept. Tokens are estimates of visible call
          content, not billed usage. Durations are measured per call where
          retained.
        </p>
        <ErrorNotice message={error} />
        {!calls && !error && <Loading />}
        {calls && !calls.length && (
          <p className="muted small">No tool calls recorded in this session.</p>
        )}
        {calls && calls.length > 0 && (
          <>
            <div className="mix-table-wrap">
              <table className="mix-table">
                <caption className="sr-only">
                  Tool calls by group. Select a group to focus the sequence and
                  top calls.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Group</th>
                    <th scope="col">Calls</th>
                    <th scope="col">Est. tokens</th>
                    <th scope="col">Measured time</th>
                    <th scope="col">Via shell</th>
                    <th scope="col">Failed</th>
                  </tr>
                </thead>
                <tbody>
                  {GROUPS.map(([group]) => {
                    const members = calls.filter(
                      (call) => call.group === group,
                    );
                    if (!members.length) return null;
                    const groupTokens = sum(members.map((call) => call.tokens));
                    const timed = members.filter(
                      (call) => call.durationMs !== null,
                    );
                    const concepts = [
                      ...new Set(members.map((c) => c.concept)),
                    ];
                    return (
                      <tr key={group} data-focused={focus === group}>
                        <th scope="row">
                          <button
                            type="button"
                            className="mix-group"
                            aria-pressed={focus === group}
                            title={concepts.join(", ")}
                            onClick={() =>
                              setFocus(focus === group ? null : group)
                            }
                          >
                            <i className={`swatch group-${group}`} />
                            {group}
                          </button>
                        </th>
                        <td data-label="Calls">
                          {number(members.length)}
                          <span className="mix-share">
                            {Math.round((members.length / calls.length) * 100)}%
                          </span>
                        </td>
                        <td data-label="Est. tokens">
                          {tokens(groupTokens)}
                          <span className="mix-share">
                            {totalTokens
                              ? `${Math.round((groupTokens / totalTokens) * 100)}%`
                              : ""}
                          </span>
                        </td>
                        <td data-label="Measured time">
                          {timed.length
                            ? seconds(sum(timed.map((call) => call.durationMs)))
                            : "—"}
                          {timed.length > 0 &&
                            timed.length < members.length && (
                              <span className="mix-share">
                                {timed.length} of {members.length}
                              </span>
                            )}
                        </td>
                        <td data-label="Via shell">
                          {number(
                            members.filter((call) => call.viaShell).length,
                          )}
                        </td>
                        <td data-label="Failed">
                          {number(members.filter((call) => call.failed).length)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {truncated && (
              <p className="coverage-note">
                Showing the first {number(calls.length)} tool calls. Later calls
                are not included in this panel.
              </p>
            )}
            <Sequence
              calls={calls}
              focus={focus}
              reference={reference}
              link={link}
            />
            <div className="top-calls">
              {(
                [
                  [
                    "Most tokens",
                    topTokens,
                    (c: Call) => `${tokens(c.tokens!)} tok`,
                  ],
                  [
                    "Longest measured",
                    topDuration,
                    (c: Call) => seconds(c.durationMs!),
                  ],
                ] as const
              ).map(([title, list, value]) => (
                <div key={title}>
                  <h3>
                    {title}
                    {focus ? ` · ${focus}` : ""}
                  </h3>
                  {!list.length && (
                    <p className="muted small">Not measured for these calls.</p>
                  )}
                  <ol>
                    {list.map((call) => (
                      <li key={call.item.item_id}>
                        <EvidenceLink reference={link(call)}>
                          {call.concept} · {label(call)}
                        </EvidenceLink>
                        <span className="small muted">{value(call)}</span>
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          </>
        )}
        <Composition
          stats={stats.data?.result}
          loading={stats.loading}
          error={stats.error}
        />
      </div>
    </section>
  );
}

type TurnCalls = { turnId: string; sessionId: string; calls: Call[] };

function byTurn(calls: Call[]) {
  const turns: TurnCalls[] = [];
  for (const call of calls) {
    const last = turns.at(-1);
    if (last?.turnId === call.item.turn_id) last.calls.push(call);
    else
      turns.push({
        turnId: call.item.turn_id,
        sessionId: call.item.session_id,
        calls: [call],
      });
  }
  return turns;
}

const time = (value: unknown) =>
  typeof value === "string"
    ? new Date(value).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "time unavailable";

function Sequence({
  calls,
  focus,
  reference,
  link,
}: {
  calls: Call[];
  focus: Group | null;
  reference: CanonicalReference;
  link: (call: Call) => CanonicalReference;
}) {
  // An explicit choice is remembered per browser; otherwise size decides.
  const [mode, setMode] = useState<"call" | "turn">(() => {
    try {
      const saved = localStorage.getItem(SEQUENCE_KEY);
      if (saved === "call" || saved === "turn") return saved;
    } catch {
      // Storage may be unavailable; fall back to the size default.
    }
    return calls.length > PER_CALL_LIMIT ? "turn" : "call";
  });
  const turns = byTurn(calls);
  const investigationId = new URLSearchParams(location.hash.slice(1)).get(
    "investigation_id",
  );
  let position = 0;
  return (
    <>
      <div className="section-heading sequence-heading">
        <h3>Sequence</h3>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          value={mode}
          aria-label="Sequence detail"
          onValueChange={(value) => {
            if (value !== "call" && value !== "turn") return;
            setMode(value);
            try {
              localStorage.setItem(SEQUENCE_KEY, value);
            } catch {
              // The choice still applies to this view.
            }
          }}
        >
          <ToggleGroupItem value="call">Per call</ToggleGroupItem>
          <ToggleGroupItem value="turn">Per turn</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <p className="small muted">
        {number(turns.length)} turn{turns.length === 1 ? "" : "s"} with tool
        calls, in source order.{" "}
        {mode === "call"
          ? "Each block opens its call; gaps separate turns."
          : "Each bar opens its turn; width follows its call count."}
      </p>
      <div className="family-legend" aria-label="Sequence colors">
        {FAMILIES.map(([family, swatch, groups]) => {
          const present = groups.filter((group) =>
            calls.some((call) => call.group === group),
          );
          if (!present.length) return null;
          return (
            <span key={family}>
              <i className={`swatch group-${swatch}`} />
              {family === present[0] && present.length === 1
                ? family
                : `${family} (${present.join(", ")})`}
            </span>
          );
        })}
      </div>
      <div
        className="call-strip"
        data-mode={mode}
        aria-label={
          mode === "call"
            ? "Tool calls in source order"
            : "Turns in source order"
        }
      >
        {turns.map((turn) => {
          const current = reference.turn_id === turn.turnId;
          const start = position;
          position += turn.calls.length;
          const title = `Turn at ${time(turn.calls[0].item.started_at)} · ${number(turn.calls.length)} call${turn.calls.length === 1 ? "" : "s"}`;
          if (mode === "call")
            return (
              <span
                key={turn.turnId}
                className="turn-cluster"
                data-current={current}
                title={title}
              >
                {turn.calls.map((call, index) => (
                  <a
                    key={call.item.item_id}
                    className={`group-${call.group}`}
                    data-dimmed={Boolean(focus && call.group !== focus)}
                    data-current={reference.item_id === call.item.item_id}
                    href={referenceLink(link(call), investigationId)}
                    title={`${start + index + 1}. ${call.concept} · ${label(call)}`}
                    aria-label={`Call ${start + index + 1}: ${call.concept}`}
                  />
                ))}
              </span>
            );
          const counts = GROUPS.map(
            ([group]) =>
              [
                group,
                turn.calls.filter((call) => call.group === group).length,
              ] as const,
          ).filter(([, count]) => count);
          return (
            <a
              key={turn.turnId}
              className="turn-bar"
              data-current={current}
              style={{ width: `${6 + turn.calls.length * 3}px` }}
              href={referenceLink(
                {
                  session_id: turn.sessionId,
                  turn_id: turn.turnId,
                  view_manifest_sha256: reference.view_manifest_sha256,
                },
                investigationId,
              )}
              title={`${title}: ${counts.map(([group, count]) => `${group} ${count}`).join(", ")}`}
              aria-label={title}
            >
              {counts.map(([group, count]) => (
                <span
                  key={group}
                  className={`group-${group}`}
                  data-dimmed={Boolean(focus && group !== focus)}
                  style={{ flexGrow: count }}
                />
              ))}
            </a>
          );
        })}
      </div>
    </>
  );
}

function Composition({
  stats,
  loading,
  error,
}: {
  stats?: SessionStatsResponse;
  loading: boolean;
  error?: string;
}) {
  const context = object(stats?.context_window);
  const categories = rows(context.categories);
  const total =
    typeof context.used_tokens === "number"
      ? context.used_tokens
      : sum(
          categories.map((c) => (typeof c.tokens === "number" ? c.tokens : 0)),
        );
  const [open, setOpen] = useState(false);
  function render(category: Record<string, unknown>, depth: number) {
    const value = typeof category.tokens === "number" ? category.tokens : 0;
    return (
      <div key={string(category.key)}>
        <div className="composition-row">
          <span style={{ paddingLeft: depth * 14 }}>
            {string(category.label) || string(category.key)}
          </span>
          <span className="composition-bar">
            <span style={{ width: `${total ? (value / total) * 100 : 0}%` }} />
          </span>
          <span className="small">{tokens(value)}</span>
        </div>
        {(depth < 1 || open) &&
          rows(category.children).map((child) => render(child, depth + 1))}
      </div>
    );
  }
  return (
    <div className="composition">
      <div className="section-heading">
        <h3>Context composition</h3>
        {categories.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
            {open ? "Collapse" : "Show all categories"}
          </Button>
        )}
      </div>
      <p className="small muted">
        {total ? `${tokens(total)} est. tokens` : ""}
        {typeof context.used_percent === "number"
          ? ` · ${context.used_percent}% of the context window`
          : ""}{" "}
        · from session.stats
      </p>
      <ErrorNotice message={error} />
      {loading && <Loading />}
      {!loading && !error && !categories.length && (
        <p className="muted small">Context composition unavailable.</p>
      )}
      {categories.map((category) => render(category, 0))}
    </div>
  );
}
