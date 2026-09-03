import * as React from "react";
import type { ApexOptions } from "apexcharts";
import type {
  CacheBreakRecord,
  CacheBreakSummary,
  CompactionSummary,
  ContextCategory,
  ContextWindowPayload,
} from "@/api";
import {
  cacheBreakTone,
  formatCostUsd,
  formatIdleSeconds,
  formatTokens,
} from "@/lib/cache-breaks";
import { ApexChart, escapeHtml, tooltipRow, useApexTheme } from "@/components/ui/apex-chart";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { categoryDotStyle } from "./shared";

/**
 * Cache breaks, compactions, provider usage observations, and warnings —
 * collapsed by default so pressure signals stay visible without dominating.
 */
export function ContextMaintenanceDetails({ payload }: { payload: ContextWindowPayload }) {
  const breaks = payload.cache_breaks;
  const compaction = payload.compaction;
  const buckets = payload.provider_usage_buckets;
  const warnings = payload.warnings;

  const sections = [
    breaks && breaks.count > 0 ? "breaks" : null,
    compaction && compaction.count > 0 ? "compactions" : null,
    buckets.length > 0 ? "buckets" : null,
    warnings.length > 0 ? "warnings" : null,
  ].filter((key): key is string => key != null);

  if (sections.length === 0) return null;

  return (
    <section aria-label="Context maintenance details">
      <Accordion type="multiple" className="grid gap-2">
        {breaks && breaks.count > 0 ? (
          <MaintenanceItem
            value="breaks"
            title="Cache breaks"
            summary={`${breaks.count} turn${breaks.count === 1 ? "" : "s"} · ${formatTokens(breaks.total_re_read_tokens)} affected${
              breaks.estimated_waste_usd != null ? ` · ${formatCostUsd(breaks.estimated_waste_usd)} est. premium` : ""
            }`}
          >
            <CacheBreakRows cacheBreaks={breaks} />
          </MaintenanceItem>
        ) : null}
        {compaction && compaction.count > 0 ? (
          <MaintenanceItem
            value="compactions"
            title="Compactions"
            summary={`${compaction.count} event${compaction.count === 1 ? "" : "s"}${
              compaction.cumulative_dropped_tokens != null
                ? ` · ${formatTokens(compaction.cumulative_dropped_tokens)} dropped`
                : ""
            }`}
          >
            <CompactionDetails compaction={compaction} />
          </MaintenanceItem>
        ) : null}
        {buckets.length > 0 ? (
          <MaintenanceItem
            value="buckets"
            title="Provider usage observations"
            summary={`${buckets.length} bucket${buckets.length === 1 ? "" : "s"} reported separately from composition`}
          >
            <ul className="m-0 grid list-none gap-1.5 p-0">
              {buckets.map((bucket) => (
                <ProviderBucketRow key={bucket.id} bucket={bucket} />
              ))}
            </ul>
          </MaintenanceItem>
        ) : null}
        {warnings.length > 0 ? (
          <MaintenanceItem value="warnings" title="Warnings" summary={String(warnings.length)}>
            <ul className="m-0 grid list-disc gap-1.5 pl-5 text-body-sm text-muted-foreground">
              {warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </MaintenanceItem>
        ) : null}
      </Accordion>
    </section>
  );
}

function MaintenanceItem({
  value,
  title,
  summary,
  children,
}: {
  value: string;
  title: string;
  summary: string;
  children: React.ReactNode;
}) {
  return (
    <AccordionItem value={value} className="rounded-lg border border-border-soft bg-card px-3 last:border-b">
      <AccordionTrigger className="items-center gap-3 py-2.5 text-body-sm hover:no-underline">
        <span className="min-w-0 flex-1 truncate text-left font-medium">{title}</span>
        <span className="mono shrink-0 text-caption text-muted-foreground">{summary}</span>
      </AccordionTrigger>
      <AccordionContent className="pb-3">{children}</AccordionContent>
    </AccordionItem>
  );
}

function CacheBreakRows({ cacheBreaks }: { cacheBreaks: CacheBreakSummary }) {
  return (
    <ol className="m-0 grid list-none gap-1.5 p-0">
      {cacheBreaks.events.map((record, index) => (
        <CacheBreakRow key={`${record.turn_id}-${index}`} record={record} />
      ))}
    </ol>
  );
}

function CacheBreakRow({ record }: { record: CacheBreakRecord }) {
  const tone = cacheBreakTone(record.type, record.effort_from, record.effort_to);
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border-soft bg-surface-subtle px-2.5 py-2 text-body-sm">
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0 text-caption",
          tone.className,
        )}
      >
        {tone.icon}
        {tone.label}
      </span>
      <span className="mono text-caption text-muted-foreground">turn {record.turn_id.slice(0, 10)}</span>
      <span className="ml-auto flex items-center gap-3 mono text-caption text-muted-foreground">
        <span>{formatTokens(record.re_read_tokens)} cache-hit loss</span>
        <span>{formatIdleSeconds(record.idle_seconds)} idle</span>
        {record.est_cost_usd != null ? <span>{formatCostUsd(record.est_cost_usd)}</span> : null}
      </span>
    </li>
  );
}

function ProviderBucketRow({ bucket }: { bucket: ContextCategory }) {
  return (
    <li className="flex items-center gap-2 text-body-sm">
      <span className="inline-block h-2 w-2 shrink-0 rounded-[2px]" style={categoryDotStyle(bucket.category)} />
      <span className="min-w-0 flex-1 truncate" title={bucket.label}>
        {bucket.label}
      </span>
      <span className="mono shrink-0 text-muted-foreground">{formatTokens(bucket.tokens.value)}</span>
      <span className="shrink-0 text-caption text-muted-foreground">{bucket.tokens.confidence}</span>
    </li>
  );
}

function CompactionDetails({ compaction }: { compaction: CompactionSummary }) {
  const theme = useApexTheme();
  const chartEvents = compaction.events.filter(
    (event) => event.pre_tokens != null || event.post_tokens != null,
  );
  // Trend chart only when multiple comparable observations exist; a single
  // observation is a row, not a chart.
  const showChart = chartEvents.length > 1;

  const options = React.useMemo<ApexOptions | null>(() => {
    if (!showChart) return null;
    return {
      stroke: { curve: "smooth", width: 2 },
      markers: { size: 4 },
      dataLabels: { enabled: false },
      xaxis: {
        categories: compaction.events.map((event) => formatCompactionTimestamp(event.timestamp)),
        labels: { hideOverlappingLabels: true, style: { fontSize: "11px" } },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { formatter: (value) => formatTokens(Number(value)) } },
      legend: { show: true, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        custom: ({ dataPointIndex }) => {
          const event = compaction.events[dataPointIndex];
          if (!event) return "";
          const rows = [
            tooltipRow("Trigger", escapeHtml(event.trigger ?? "auto"), theme.axis),
            tooltipRow("Mechanism", escapeHtml(event.mechanism.replaceAll("_", " ")), theme.axis),
            tooltipRow(
              "Window",
              event.pre_tokens != null && event.post_tokens != null
                ? `${formatTokens(event.pre_tokens)} → ${formatTokens(event.post_tokens)}`
                : "size not exposed",
              theme.axis,
            ),
            tooltipRow(
              "Dropped",
              event.dropped_tokens != null ? formatTokens(event.dropped_tokens) : "-",
              theme.axis,
            ),
          ].join("");
          return `<div style="padding:10px 12px;min-width:210px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(formatCompactionTimestamp(event.timestamp))}</div>${rows}</div>`;
        },
      },
    };
  }, [compaction.events, showChart, theme]);

  return (
    <div className="grid gap-2">
      {showChart && options ? (
        <>
          <ApexChart
            type="line"
            series={[
              { name: "Before", data: compaction.events.map((event) => event.pre_tokens ?? null) },
              { name: "After", data: compaction.events.map((event) => event.post_tokens ?? null) },
            ]}
            options={options}
            height={220}
            ariaLabel="Context tokens before and after each compaction"
          />
          <ul className="sr-only">
            {compaction.events.map((event, index) => (
              <li key={`${event.timestamp}-${index}`}>{compactionRowText(event)}</li>
            ))}
          </ul>
        </>
      ) : (
        <ol className="m-0 grid list-none gap-1.5 p-0">
          {compaction.events.map((event, index) => (
            <li
              key={`${event.timestamp}-${index}`}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border-soft bg-surface-subtle px-2.5 py-2 text-body-sm"
            >
              <Badge variant="outline" className="shrink-0 px-1.5 py-0 text-caption text-foreground">
                {event.trigger ?? "auto"}
              </Badge>
              <span className="min-w-0 flex-1 truncate text-body-sm">
                {event.mechanism.replaceAll("_", " ")}
              </span>
              <span className="mono shrink-0 text-caption text-muted-foreground">
                {formatCompactionTimestamp(event.timestamp)}
              </span>
              <span className="mono shrink-0 text-caption text-muted-foreground">
                {compactionDeltaLabel(event)}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function compactionDeltaLabel(event: CompactionSummary["events"][number]) {
  if (event.pre_tokens != null && event.post_tokens != null) {
    return `${formatTokens(event.pre_tokens)} → ${formatTokens(event.post_tokens)}`;
  }
  if (event.dropped_tokens != null) return `${formatTokens(event.dropped_tokens)} dropped`;
  // ``context_compacted`` (Codex) exposes no pre/post/dropped; say so instead
  // of implying a measured outcome.
  return "size not exposed";
}

function compactionRowText(event: CompactionSummary["events"][number]) {
  return `${formatCompactionTimestamp(event.timestamp)}: ${event.trigger ?? "auto"}, ${compactionDeltaLabel(event)}`;
}

function formatCompactionTimestamp(value: string) {
  if (!value) return "-";
  // Truncate to ``YYYY-MM-DD HH:MM`` (UTC) for compactness.
  return value.slice(0, 16).replace("T", " ");
}
