import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchCalibration,
  fetchForecasts,
  type CalibrationResponse,
  type ForecastKind,
  type ForecastListResponse,
} from "@/api";
import { CalibrationCohortCard } from "@/components/calibration-cohort-card";
import { ForecastTable } from "@/components/forecast-table";

const KIND_OPTIONS: { value: ForecastKind | ""; label: string }[] = [
  { value: "", label: "All kinds" },
  { value: "historical_backcast", label: "Backcasts" },
  { value: "prospective", label: "Prospective" },
  { value: "prospective_unbound", label: "Unbound" },
  { value: "runtime_advisory", label: "Advisory" },
];

const HARNESS_OPTIONS = ["", "codex_cli", "claude_code", "pi"];

function FilterPills({
  options,
  value,
  onChange,
  formatLabel,
}: {
  options: string[];
  value: string;
  onChange: (value: string) => void;
  formatLabel?: (value: string) => string;
}) {
  return (
    <div className="flex gap-1 rounded-lg border border-border bg-secondary/50 p-1">
      {options.map((option) => (
        <button
          key={option || "all"}
          onClick={() => onChange(option)}
          className={`rounded-md px-3 py-1 text-caption font-display transition-colors ${
            value === option
              ? "bg-primary text-primary-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {formatLabel ? formatLabel(option) : option || "All"}
        </button>
      ))}
    </div>
  );
}

export function ForecastsRoute() {
  const [kind, setKind] = React.useState<ForecastKind | "">("");
  const [harness, setHarness] = React.useState("");

  const filters = {
    kind: kind || undefined,
    target_harness_name: harness || undefined,
  };

  const calibrationQuery = useQuery<CalibrationResponse>({
    queryKey: ["calibration", filters],
    queryFn: () => fetchCalibration(filters),
  });
  const forecastsQuery = useQuery<ForecastListResponse>({
    queryKey: ["forecasts", filters],
    queryFn: () => fetchForecasts({ ...filters, limit: 100 }),
  });

  const isLoading = calibrationQuery.isLoading || forecastsQuery.isLoading;
  const error = calibrationQuery.error ?? forecastsQuery.error;
  const cohorts = calibrationQuery.data?.cohorts ?? [];
  const forecasts = forecastsQuery.data?.items ?? [];

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-6">
        <h1 className="font-display text-heading font-semibold tracking-tight">
          Forecast Calibration
        </h1>
        <p className="mt-1 max-w-3xl text-caption text-muted-foreground">
          Agent duration forecasts versus measured wall-clock actuals. Historical backcasts
          are development evidence, not prospective calibration; the populations are never
          merged. Statistics are undefined rather than guessed when a cohort lacks samples.
        </p>
      </div>

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <FilterPills
          options={KIND_OPTIONS.map((option) => option.value)}
          value={kind}
          onChange={(value) => setKind(value as ForecastKind | "")}
          formatLabel={(value) =>
            KIND_OPTIONS.find((option) => option.value === value)?.label ?? value
          }
        />
        <FilterPills
          options={HARNESS_OPTIONS}
          value={harness}
          onChange={setHarness}
          formatLabel={(value) => (value ? value.replace("_", " ") : "All harnesses")}
        />
      </div>

      {isLoading && (
        <div className="space-y-4">
          {[0, 1].map((index) => (
            <div
              key={index}
              className="h-44 animate-shimmer rounded-xl border border-border bg-card"
            />
          ))}
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-body-sm text-destructive">
          <p className="font-medium">Failed to load calibration data</p>
          <p className="mt-1 text-caption">{String(error)}</p>
          <button
            onClick={() => {
              calibrationQuery.refetch();
              forecastsQuery.refetch();
            }}
            className="mt-2 rounded-md bg-destructive/10 px-3 py-1 text-caption hover:bg-destructive/20"
          >
            Retry
          </button>
        </div>
      )}

      {!isLoading && !error && (
        <>
          {cohorts.length === 0 ? (
            <div className="rounded-xl border border-border bg-card p-8 text-center">
              <p className="text-body-sm text-muted-foreground">
                No calibration cohorts yet for the current filters.
              </p>
              <p className="mt-2 text-caption text-muted-foreground">
                Generate historical backcasts with{" "}
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                  ct plugin code-time forecast backfill
                </code>{" "}
                or one forecast with{" "}
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                  ct plugin code-time forecast predict --turn-id &lt;id&gt;
                </code>
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {cohorts.map((cohort, index) => (
                <CalibrationCohortCard key={index} cohort={cohort} />
              ))}
            </div>
          )}

          <div className="mt-8 rounded-xl border border-border bg-card shadow-sm">
            <div className="border-b border-border px-5 py-3">
              <h2 className="font-display text-body-sm font-medium tracking-wide">
                Forecast Records
              </h2>
              <p className="mt-0.5 text-caption text-muted-foreground">
                Individual artifacts behind the aggregates above — a single record is not
                a performance conclusion.
              </p>
            </div>
            <ForecastTable forecasts={forecasts} />
          </div>
        </>
      )}
    </div>
  );
}
