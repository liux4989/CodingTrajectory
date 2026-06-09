import * as React from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Pin, PinOff } from "lucide-react";
import {
  fetchContextWindow,
  type ContextCategory,
  type ContextEvent,
  type TokenEvidence,
} from "../api";
import { Button } from "../components/ui/button";
import { StateBlock } from "../components/state-block";

function formatTokens(value: number | null | undefined) {
  if (value == null) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function groupLabel(event: ContextEvent) {
  if (event.group === "before_first_prompt") return "Before first prompt";
  if (event.group === "post_turn") return "After final turn";
  return `Turn ${event.turn_id ?? "-"}`;
}

function evidenceLabel(evidence: TokenEvidence | null) {
  if (!evidence) return "No event-level token evidence";
  return `${formatTokens(evidence.value)} tokens, ${evidence.confidence.replaceAll("_", " ")}`;
}

function categoryTotal(categories: ContextCategory[]) {
  return categories.reduce((total, category) => total + category.tokens.value, 0);
}

export function ContextWindowRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/context-window" });
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
  });
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [hoveredId, setHoveredId] = React.useState<string | null>(null);
  const [pinnedId, setPinnedId] = React.useState<string | null>(null);
  const eventRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const events = query.data?.events ?? [];

  React.useEffect(() => {
    if (!selectedId && events[0]) setSelectedId(events[0].id);
  }, [events, selectedId]);

  const activeId = pinnedId ?? hoveredId ?? selectedId ?? events[0]?.id ?? null;
  const activeEvent = events.find((event) => event.id === activeId) ?? null;

  function moveFocus(index: number, direction: -1 | 1) {
    const next = Math.min(Math.max(index + direction, 0), events.length - 1);
    eventRefs.current[next]?.focus();
    setSelectedId(events[next]?.id ?? null);
  }

  if (query.isPending) {
    return <StateBlock title="Loading context window" detail="Reading normalized session projections." />;
  }
  if (query.isError) {
    return <StateBlock title="Context window failed" detail={query.error.message} />;
  }

  const payload = query.data;
  const total = categoryTotal(payload.categories);

  return (
    <div className="route-stack context-route">
      <header className="context-header">
        <div>
          <Link to="/sessions" className="back-link"><ArrowLeft size={16} /> Sessions</Link>
          <p className="eyebrow">Context inspector</p>
          <h2>{payload.model ?? "Unknown model"}</h2>
          <p className="context-subtitle">
            <code>{payload.session_id}</code> · {payload.vendor} · {formatTokens(payload.used_tokens?.value)}
            {payload.used_percent != null ? ` (${payload.used_percent.toFixed(1)}%)` : ""} used
          </p>
        </div>
      </header>

      <figure className="context-composition card">
        <figcaption>
          <span>Observed context composition</span>
          <strong>{formatTokens(payload.context_window_tokens?.value)} window</strong>
        </figcaption>
        <div className="context-bar" aria-label="Context category composition">
          {payload.categories.map((category) => (
            <span
              key={category.id}
              className="context-bar-segment"
              data-category={category.category}
              style={{ inlineSize: `${total ? (category.tokens.value / total) * 100 : 0}%` }}
              title={`${category.label}: ${formatTokens(category.tokens.value)} tokens`}
            />
          ))}
        </div>
        <ul className="context-legend" role="list">
          {payload.categories.map((category) => (
            <li key={category.id}>
              <span className="category-swatch" data-category={category.category} />
              <span>{category.label}</span>
              <strong>{formatTokens(category.tokens.value)}</strong>
              <small>{category.tokens.confidence.replaceAll("_", " ")}</small>
            </li>
          ))}
        </ul>
      </figure>

      {payload.warnings.length ? (
        <aside className="context-warnings" aria-label="Attribution warnings">
          <strong>Attribution limits</strong>
          <ul>{payload.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </aside>
      ) : null}

      <div className="context-inspector">
        <section className="event-stream" aria-labelledby="event-stream-title">
          <div className="event-stream-heading">
            <div>
              <p className="eyebrow">Trajectory</p>
              <h3 id="event-stream-title">Context events</h3>
            </div>
            <span>{events.length} rows</span>
          </div>
          <div className="event-list" onMouseLeave={() => setHoveredId(null)}>
            {events.map((event, index) => {
              const previous = events[index - 1];
              const startsGroup = !previous || previous.group !== event.group || previous.turn_id !== event.turn_id;
              const isActive = event.id === activeId;
              return (
                <React.Fragment key={event.id}>
                  {startsGroup ? <h4>{groupLabel(event)}</h4> : null}
                  <button
                    ref={(node) => { eventRefs.current[index] = node; }}
                    type="button"
                    className={`event-row ${isActive ? "is-active" : ""}`}
                    aria-pressed={event.id === selectedId}
                    onMouseEnter={() => setHoveredId(event.id)}
                    onFocus={() => setHoveredId(event.id)}
                    onBlur={() => setHoveredId(null)}
                    onClick={() => setSelectedId(event.id)}
                    onKeyDown={(eventKey) => {
                      if (eventKey.key === "ArrowDown") {
                        eventKey.preventDefault();
                        moveFocus(index, 1);
                      } else if (eventKey.key === "ArrowUp") {
                        eventKey.preventDefault();
                        moveFocus(index, -1);
                      }
                    }}
                  >
                    <span className="event-marker" data-category={event.category} />
                    <span className="event-copy">
                      <strong>{event.label}</strong>
                      <span>{event.summary ?? event.source}</span>
                    </span>
                    <span className="event-tokens">
                      {event.tokens ? `+${formatTokens(event.tokens.value)}` : "-"}
                    </span>
                  </button>
                </React.Fragment>
              );
            })}
          </div>
        </section>

        <aside className="event-detail card">
          {activeEvent ? (
            <>
              <div className="event-detail-heading">
                <div>
                  <p className="eyebrow">{activeEvent.category.replaceAll("_", " ")}</p>
                  <h3>{activeEvent.label}</h3>
                </div>
                <Button
                  size="sm"
                  variant={pinnedId === activeEvent.id ? "default" : "secondary"}
                  onClick={() => setPinnedId((current) => current === activeEvent.id ? null : activeEvent.id)}
                  aria-pressed={pinnedId === activeEvent.id}
                >
                  {pinnedId === activeEvent.id ? <PinOff size={15} /> : <Pin size={15} />}
                  {pinnedId === activeEvent.id ? "Unpin" : "Pin"}
                </Button>
              </div>
              <p className="event-detail-summary">{activeEvent.summary ?? "No text preview is available."}</p>
              <dl className="event-evidence">
                <div><dt>Token impact</dt><dd>{evidenceLabel(activeEvent.tokens)}</dd></div>
                <div><dt>Evidence source</dt><dd>{activeEvent.tokens?.source ?? activeEvent.source}</dd></div>
                <div><dt>Event confidence</dt><dd>{activeEvent.confidence.replaceAll("_", " ")}</dd></div>
                <div><dt>Terminal visibility</dt><dd>{activeEvent.terminal_visible ? "Visible" : "Hidden"}</dd></div>
                {Object.entries(activeEvent.detail_ref).map(([key, value]) => (
                  <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd><code>{value}</code></dd></div>
                ))}
              </dl>
            </>
          ) : <p>No event selected.</p>}
        </aside>
      </div>
    </div>
  );
}
