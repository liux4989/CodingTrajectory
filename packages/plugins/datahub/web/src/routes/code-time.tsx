import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchCodeTimeCalibration,
  fetchCodeTimeForecasts,
  fetchCodeTimeReport,
  type CodeTimeWindow,
  type ForecastKind,
} from "@/api";
import { formatCompactNumber, formatCostUsd, formatDuration } from "@/lib/format";
import { PageHeader } from "@/components/route-header";
import { MetricCard } from "@/components/metric-card";
import { StaggerGroup } from "@/components/stagger-group";
import { StateBlock } from "@/components/state-block";
import { LoadingShell } from "@/components/loading-shell";
import { SectionTabs } from "@/components/section-tabs";
import { CodeTimeProjectTable } from "@/components/code-time-project-table";
import { ForecastTable } from "@/components/forecast-table";
import { CalibrationCohortCard } from "@/components/calibration-cohort-card";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

const WINDOW_OPTIONS: { value: CodeTimeWindow; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "72h", label: "72h" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
];

const KIND_OPTIONS: { value: ForecastKind | "all"; label: string }[] = [
  { value: "all", label: "All kinds" },
  { value: "historical_backcast", label: "Backcasts" },
  { value: "prospective", label: "Prospective" },
  { value: "prospective_unbound", label: "Unbound" },
  { value: "runtime_advisory", label: "Advisory" },
];

const HARNESS_OPTIONS = ["all", "codex_cli", "claude_code", "pi"];

export function CodeTimeRoute() {
  const [window, setWindow] = React.useState<CodeTimeWindow>("today");
  const [activeTab, setActiveTab] = React.useState("projects");
  const [kind, setKind] = React.useState<ForecastKind | "all">("all");
  const [harness, setHarness] = React.useState("all");

  const report = useQuery({
    queryKey: ["code-time", "report", window],
    queryFn: () => fetchCodeTimeReport({ window }),
    placeholderData: (previous) => previous,
  });

  const forecastFilters = {
    kind: kind === "all" ? undefined : kind,
    targetHarnessName: harness === "all" ? undefined : harness,
  };
  const forecasts = useQuery({
    queryKey: ["code-time", "forecasts", kind, harness],
    queryFn: () => fetchCodeTimeForecasts(forecastFilters),
    enabled: activeTab === "forecasts",
    placeholderData: (previous) => previous,
  });
  const calibration = useQuery({
    queryKey: ["code-time", "calibration", kind, harness],
    queryFn: () => fetchCodeTimeCalibration(forecastFilters),
    enabled: activeTab === "forecasts",
    placeholderData: (previous) => previous,
  });

  if (report.isPending) {
    return <LoadingShell eyebrow="Code time" title="Loading code time report" variant="mixed" />;
  }

  if (report.isError) {
    return (
      <StateBlock
        title="Code time report unavailable"
        detail={report.error.message}
        onRetry={() => report.refetch()}
      />
    );
  }

  const totals = report.data.totals;

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <PageHeader
        eyebrow="Analyze"
        title="Code Time"
        description="Time, sessions, and cost across projects."
        actions={
          <ToggleGroup
            type="single"
            value={window}
            onValueChange={(value) => {
              if (value === "today" || value === "72h" || value === "7d" || value === "30d") {
                setWindow(value);
              }
            }}
            variant="outline"
          >
            {WINDOW_OPTIONS.map((option) => (
              <ToggleGroupItem key={option.value} value={option.value}>
                {option.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        }
      />
      <SectionTabs
        ariaLabel="Code time sections"
        activeTab={activeTab}
        onTabChange={setActiveTab}
        summary={
          <section className="stat-grid min-w-0">
            <StaggerGroup className="contents">
              <MetricCard
                label="Sessions"
                value={totals.session_count}
                detail={`${totals.project_count} active project${totals.project_count === 1 ? "" : "s"}`}
              />
              <MetricCard
                label="Coding time"
                value={formatDuration(totals.execution_seconds)}
                detail={`${formatDuration(totals.wait_seconds)} waiting on agents`}
              />
              <MetricCard
                label="Turns"
                value={totals.turns}
                detail={`${totals.tool_calls.toLocaleString()} tool calls`}
              />
              <MetricCard
                label="Tokens"
                value={formatCompactNumber(totals.tokens.processed_tokens)}
                detail={`${formatCostUsd(totals.cost_usd)} estimated cost`}
              />
            </StaggerGroup>
          </section>
        }
        tabs={[
          {
            id: "projects",
            label: "Projects",
            badge: report.data.projects.length,
            content: (
              <Card className="min-w-0">
                <CardHeader>
                  <CardTitle className="title-card">Time by Project</CardTitle>
                  <CardDescription>
                    Sessions, coding time, tokens, and cost per project for the selected window.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <CodeTimeProjectTable projects={report.data.projects} />
                </CardContent>
              </Card>
            ),
          },
          {
            id: "forecasts",
            label: "Forecasts",
            content: (
              <div className="grid gap-4">
                <Card className="min-w-0">
                  <CardHeader>
                    <CardTitle className="title-card">Duration Forecasts</CardTitle>
                    <CardDescription>
                      Kind-labeled forecast artifacts versus measured actuals. Historical backcasts are
                      not prospective calibration evidence.
                    </CardDescription>
                    <div className="flex flex-wrap gap-2 pt-2">
                      <ToggleGroup
                        type="single"
                        value={kind}
                        onValueChange={(value) => {
                          if (value) setKind(value as ForecastKind | "all");
                        }}
                        variant="outline"
                      >
                        {KIND_OPTIONS.map((option) => (
                          <ToggleGroupItem key={option.value} value={option.value}>
                            {option.label}
                          </ToggleGroupItem>
                        ))}
                      </ToggleGroup>
                      <ToggleGroup
                        type="single"
                        value={harness}
                        onValueChange={(value) => {
                          if (value) setHarness(value);
                        }}
                        variant="outline"
                      >
                        {HARNESS_OPTIONS.map((option) => (
                          <ToggleGroupItem key={option} value={option}>
                            {option === "all" ? "All harnesses" : option}
                          </ToggleGroupItem>
                        ))}
                      </ToggleGroup>
                    </div>
                  </CardHeader>
                  <CardContent>
                    {forecasts.isPending ? (
                      <LoadingShell eyebrow="Forecasts" title="Loading forecasts" variant="table" />
                    ) : forecasts.isError ? (
                      <StateBlock
                        title="Forecasts unavailable"
                        detail={forecasts.error.message}
                        onRetry={() => forecasts.refetch()}
                      />
                    ) : (
                      <ForecastTable forecasts={forecasts.data.items ?? []} />
                    )}
                  </CardContent>
                </Card>
                {calibration.isError ? (
                  <StateBlock
                    title="Calibration unavailable"
                    detail={calibration.error.message}
                    onRetry={() => calibration.refetch()}
                  />
                ) : (
                  (calibration.data?.cohorts ?? []).map((cohort, index) => (
                    <CalibrationCohortCard key={`${cohort.cohort.forecast_kind}-${index}`} cohort={cohort} />
                  ))
                )}
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
