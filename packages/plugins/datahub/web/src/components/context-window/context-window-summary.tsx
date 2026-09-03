import * as React from "react";
import type { ContextWindowPayload } from "@/api";
import { formatCostUsd, formatTokens } from "@/lib/cache-breaks";
import { cn } from "@/lib/utils";
import {
  aggregateCategories,
  categoryDotStyle,
  categoryLabel,
  isEstimatedConfidence,
} from "./shared";

/**
 * Capacity status, category composition, largest contributors, and context
 * pressure — the page-level explanation shown before any event is selected.
 */
export function ContextWindowSummary({ payload }: { payload: ContextWindowPayload }) {
  const capacity = payload.context_window_tokens?.value ?? null;
  const used = payload.used_tokens?.value ?? null;
  const usedEstimated = isEstimatedConfidence(payload.used_tokens?.confidence);
  const categories = React.useMemo(() => aggregateCategories(payload.categories), [payload.categories]);
  const observed = used ?? categories.reduce((sum, item) => sum + item.tokens, 0);
  const capacityKnown = capacity != null && capacity > 0;
  // Contribution rankings are by size; the composition bar keeps the fixed
  // category order so segment positions stay stable across sessions.
  const bySize = React.useMemo(
    () => [...categories].sort((a, b) => b.tokens - a.tokens),
    [categories],
  );

  return (
    <>
      <section aria-label="Context capacity" className="grid gap-3">
        <p className="m-0 text-body-sm">
          {used == null ? (
            <span className="text-muted-foreground">
              No context observation reported{payload.model ? ` for ${payload.model}` : ""}.
            </span>
          ) : capacityKnown ? (
            <>
              <span className="mono font-semibold text-foreground">
                {usedEstimated ? "~" : ""}
                {formatTokens(used)}
              </span>{" "}
              <span className="text-muted-foreground">used of</span>{" "}
              <span className="mono font-semibold text-foreground">{formatTokens(capacity)}</span>
              {payload.used_percent != null ? (
                <span className="text-muted-foreground"> · {payload.used_percent.toFixed(1)}%</span>
              ) : null}
              <span className="text-muted-foreground">
                {" "}· <span className="mono">{formatTokens(Math.max(capacity - used, 0))}</span> remaining
              </span>
            </>
          ) : (
            <>
              <span className="mono font-semibold text-foreground">
                {usedEstimated ? "~" : ""}
                {formatTokens(used)}
              </span>{" "}
              <span className="text-muted-foreground">
                observed · capacity unavailable{payload.model ? ` for ${payload.model}` : ""}
              </span>
            </>
          )}
        </p>

        {categories.length > 0 ? (
          <>
            <div
              className="flex h-8 w-full overflow-hidden rounded-md border border-border-soft bg-surface-emphasis"
              role="img"
              aria-label={compositionAriaLabel(categories, capacityKnown ? capacity : observed, capacityKnown)}
            >
              {categories.map(({ category, tokens }) => {
                const denominator = capacityKnown ? capacity : observed;
                const widthPct = denominator > 0 ? Math.min((tokens / denominator) * 100, 100) : 0;
                if (widthPct <= 0) return null;
                return (
                  <div
                    key={category}
                    className="h-full min-w-0 border-r border-r-white/24 last:border-r-0"
                    style={{ width: `${widthPct}%`, background: categoryDotStyle(category).background }}
                  />
                );
              })}
              {capacityKnown && payload.used_percent != null && payload.used_percent < 100 ? (
                <div
                  className="h-full"
                  style={{ width: `${Math.max(100 - payload.used_percent, 0)}%` }}
                  aria-hidden="true"
                />
              ) : null}
            </div>
            <ul className="m-0 flex list-none flex-wrap gap-x-3 gap-y-1.5 p-0">
              {categories.map(({ category, tokens }) => (
                <li key={category} className="inline-flex items-center gap-1.5 text-caption text-muted-foreground">
                  <span className="inline-block h-2 w-2 rounded-[2px]" style={categoryDotStyle(category)} />
                  <span>{categoryLabel(category)}</span>
                  <span className="font-mono">{formatTokens(tokens)}</span>
                  {capacityKnown ? null : observed > 0 ? (
                    <span className="font-mono">({((tokens / observed) * 100).toFixed(0)}%)</span>
                  ) : null}
                </li>
              ))}
              {capacityKnown ? (
                <li className="inline-flex items-center gap-1.5 text-caption text-muted-foreground">
                  <span className="inline-block h-2 w-2 rounded-[2px] bg-surface-emphasis ring-1 ring-border-soft" />
                  <span>Unused</span>
                  <span className="font-mono">
                    {formatTokens(used != null ? Math.max(capacity - used, 0) : null)}
                  </span>
                </li>
              ) : null}
            </ul>
          </>
        ) : null}
      </section>

      {payload.session_sections.length > 1 ? <ScopeStrip payload={payload} /> : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <ContributorsCard payload={payload} categories={bySize} />
        <PressureCard payload={payload} categories={categories} />
      </div>
    </>
  );
}

function compositionAriaLabel(
  categories: Array<{ category: string; tokens: number }>,
  denominator: number,
  capacityKnown: boolean,
) {
  const parts = categories.map(
    ({ category, tokens }) =>
      `${categoryLabel(category)} ${formatTokens(tokens)} tokens${
        denominator > 0 ? `, ${((tokens / denominator) * 100).toFixed(1)}%` : ""
      }`,
  );
  const basis = capacityKnown ? "of the context window" : "of observed tokens";
  return `Context composition ${basis}: ${parts.join("; ")}`;
}

/** Compact strip naming each scope when a session graph has child sessions. */
function ScopeStrip({ payload }: { payload: ContextWindowPayload }) {
  return (
    <section aria-label="Session graph scopes" className="grid gap-2">
      <p className="m-0 text-caption text-muted-foreground">
        This session id resolves to a session graph. Each scope below is a separate context window; the
        composition above covers the active session only.
      </p>
      <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
        {payload.session_sections.map((section) => {
          const isActive = section.session_id === payload.active_session_id;
          return (
            <li
              key={section.session_id}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border border-border-soft bg-surface-subtle px-2 py-1 text-caption",
                isActive && "border-primary/60 bg-surface-emphasis",
              )}
            >
              <span className={cn("font-medium", isActive ? "text-foreground" : "text-muted-foreground")}>
                {section.role}
              </span>
              <span className="mono text-muted-foreground">{section.session_id.slice(0, 8)}</span>
              <span className="max-w-40 truncate text-muted-foreground" title={section.label}>
                {section.label}
              </span>
              <span className="mono text-muted-foreground">
                {formatTokens(section.used_tokens?.value)}
                {section.used_percent != null ? ` · ${section.used_percent.toFixed(1)}%` : ""}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ContributorsCard({
  payload,
  categories,
}: {
  payload: ContextWindowPayload;
  categories: Array<{ category: string; tokens: number }>;
}) {
  const top = categories.slice(0, 4);
  const expensive = payload.expensive_items.slice(0, 3);
  return (
    <section aria-labelledby="context-contributors-title" className="panel grid content-start gap-2">
      <h2 id="context-contributors-title" className="m-0 font-display text-heading">
        Largest contributors
      </h2>
      {top.length === 0 && expensive.length === 0 ? (
        <p className="m-0 text-body-sm text-muted-foreground">No context composition reported.</p>
      ) : (
        <ul className="m-0 grid list-none gap-1.5 p-0">
          {top.map(({ category, tokens }) => (
            <li key={category} className="flex items-center gap-2 text-body-sm">
              <span className="inline-block h-2 w-2 shrink-0 rounded-[2px]" style={categoryDotStyle(category)} />
              <span className="min-w-0 flex-1 truncate">{categoryLabel(category)}</span>
              <span className="mono shrink-0 text-muted-foreground">{formatTokens(tokens)}</span>
            </li>
          ))}
          {expensive.map((item) => (
            <li key={item.item_id} className="flex items-center gap-2 text-body-sm">
              <span
                className="inline-block h-2 w-2 shrink-0 rounded-[2px]"
                style={categoryDotStyle(item.category)}
              />
              <span className="min-w-0 flex-1 truncate" title={item.summary}>
                {item.label}
              </span>
              <span className="mono shrink-0 text-muted-foreground">
                {formatCostUsd(item.estimated_cost.value_usd)}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="m-0 text-caption text-muted-foreground">
        {payload.token_cost ? (
          <>
            Session cost <span className="mono">{formatCostUsd(payload.token_cost.value_usd)}</span> (
            {payload.token_cost.confidence})
          </>
        ) : (
          "Session cost unavailable"
        )}
        {payload.provider_usage_buckets.length > 0
          ? " · provider usage buckets reported separately"
          : ""}
      </p>
    </section>
  );
}

function PressureCard({
  payload,
  categories,
}: {
  payload: ContextWindowPayload;
  categories: Array<{ category: string; tokens: number }>;
}) {
  const breaks = payload.cache_breaks;
  const compaction = payload.compaction;
  const warningCount = payload.warnings.length;
  const growth = React.useMemo(() => {
    const added = categories
      .filter((item) => item.category !== "starting_context")
      .reduce((sum, item) => sum + item.tokens, 0);
    const estimated = payload.categories.some(
      (item) => item.category !== "starting_context" && isEstimatedConfidence(item.tokens.confidence),
    );
    return { added, estimated };
  }, [categories, payload.categories]);

  const lastCompaction = compaction?.events[compaction.events.length - 1] ?? null;
  const hasSignals =
    (breaks?.count ?? 0) > 0 || (compaction?.count ?? 0) > 0 || warningCount > 0 || growth.added > 0;

  return (
    <section aria-labelledby="context-pressure-title" className="panel grid content-start gap-2">
      <h2 id="context-pressure-title" className="m-0 font-display text-heading">
        Context pressure
      </h2>
      {!hasSignals ? (
        <p className="m-0 text-body-sm text-muted-foreground">
          No cache breaks, compactions, or warnings observed.
        </p>
      ) : (
        <ul className="m-0 grid list-none gap-1.5 p-0 text-body-sm">
          {breaks && breaks.count > 0 ? (
            <li className="flex items-baseline justify-between gap-2">
              <span>
                {breaks.count} cache break{breaks.count === 1 ? "" : "s"}
              </span>
              <span className="mono shrink-0 text-muted-foreground">
                {formatTokens(breaks.total_re_read_tokens)} affected
                {breaks.estimated_waste_usd != null ? ` · ${formatCostUsd(breaks.estimated_waste_usd)}` : ""}
              </span>
            </li>
          ) : null}
          {compaction && compaction.count > 0 ? (
            <li className="flex items-baseline justify-between gap-2">
              <span>
                {compaction.count} compaction{compaction.count === 1 ? "" : "s"}
              </span>
              <span className="mono shrink-0 text-muted-foreground">
                {lastCompaction?.pre_tokens != null && lastCompaction?.post_tokens != null
                  ? `${formatTokens(lastCompaction.pre_tokens)} → ${formatTokens(lastCompaction.post_tokens)}`
                  : "size not exposed"}
              </span>
            </li>
          ) : null}
          {growth.added > 0 ? (
            <li className="flex items-baseline justify-between gap-2">
              <span>Added since first prompt</span>
              <span className="mono shrink-0 text-muted-foreground">
                {growth.estimated ? "~" : ""}
                {formatTokens(growth.added)}
              </span>
            </li>
          ) : null}
          {warningCount > 0 ? (
            <li className="flex items-baseline justify-between gap-2">
              <span>
                {warningCount} warning{warningCount === 1 ? "" : "s"}
              </span>
              <span className="shrink-0 text-caption text-muted-foreground">details below</span>
            </li>
          ) : null}
        </ul>
      )}
    </section>
  );
}
