import type { CalibrationCohort } from "@/api";
import { ForecastKindBadge } from "@/components/forecast-kind-badge";
import { ApexChart } from "@/components/ui/apex-chart";

function formatNumber(value: number | "undefined" | undefined): string {
  if (value === undefined || value === "undefined") return "undefined";
  return String(value);
}

function StatCell({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="rounded-lg border border-border-subtle bg-secondary/30 px-3 py-2">
      <p className="text-eyebrow font-display uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      <p className="mt-0.5 font-display text-body-sm font-semibold tabular-nums">{value}</p>
      {detail && <p className="text-caption text-muted-foreground">{detail}</p>}
    </div>
  );
}

/**
 * One calibration cohort: aggregate statistics only, always with sample and
 * exclusion counts. Single predictions are never presented as conclusions.
 */
export function CalibrationCohortCard({ cohort }: { cohort: CalibrationCohort }) {
  const key = cohort.cohort;
  const stats = cohort.statistics;
  const exclusions = Object.entries(cohort.exclusions ?? {});
  const buckets = (cohort.buckets ?? []).filter((bucket) => bucket.sample_count > 0);

  const ratio = stats.calibration_ratio;
  const ratioDefined = ratio && ratio.value !== "undefined";
  const compression = stats.compression_exponent;

  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <ForecastKindBadge kind={key.forecast_kind} />
        <span className="font-display text-body-sm font-medium">
          {key.estimator_provider ?? "unknown provider"}
          {key.estimator_model ? ` / ${key.estimator_model}` : " / default model"}
          {key.estimator_effort ? ` / ${key.estimator_effort}` : ""}
        </span>
      </div>
      <p className="mt-1 text-caption text-muted-foreground">
        prompt {key.prompt_version ?? "?"} · schema {key.schema_version ?? "?"} · retrieval{" "}
        {key.retrieval_policy_version ?? "?"}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted-foreground">
        <span>
          eligible <span className="font-semibold text-foreground tabular-nums">{cohort.eligible_count}</span>
        </span>
        <span>
          primary <span className="font-semibold text-foreground tabular-nums">{cohort.primary_count}</span>
        </span>
        <span>
          usable{" "}
          <span className="font-semibold text-foreground tabular-nums">{stats.sample_count ?? 0}</span>
        </span>
        {exclusions.map(([reason, count]) => (
          <span key={reason} className="rounded-full bg-muted px-2 py-0.5">
            excluded {reason}: {count}
          </span>
        ))}
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <StatCell
          label="Calibration ratio"
          value={ratioDefined ? `${ratio.value}x` : "undefined"}
          detail={
            ratioDefined
              ? `95% interval [${ratio.interval_95?.[0]}, ${ratio.interval_95?.[1]}]`
              : ratio?.reason ?? "insufficient samples"
          }
        />
        <StatCell label="Median |log error|" value={formatNumber(stats.median_absolute_log_error)} />
        <StatCell label="Within 1.5x" value={formatNumber(stats.within_1_5x_share)} />
        <StatCell label="p80 coverage" value={formatNumber(stats.p80_coverage)} />
        <StatCell
          label="Compression"
          value={compression && compression.value !== "undefined" ? String(compression.value) : "undefined"}
          detail={compression?.value === "undefined" ? compression.reason : "0 = flat, 1 = tracking"}
        />
      </div>

      {buckets.length > 0 && (
        <div className="mt-4">
          <p className="mb-1 text-caption text-muted-foreground">
            Duration buckets (outcome diagnostics, not task difficulty)
          </p>
          <ApexChart
            type="bar"
            height={180}
            ariaLabel="Calibration ratio by actual-duration bucket"
            series={[
              {
                name: "calibration ratio",
                data: buckets.map((bucket) => ({
                  x: bucket.bucket,
                  y: bucket.calibration_ratio ?? 0,
                })),
              },
            ]}
            options={{
              plotOptions: { bar: { borderRadius: 3, columnWidth: "55%" } },
              dataLabels: {
                enabled: true,
                formatter: (_value: number, opts?: { dataPointIndex: number }) =>
                  `n=${buckets[opts?.dataPointIndex ?? 0]?.sample_count ?? 0}`,
              },
              yaxis: {
                min: 0,
                title: { text: "geo mean p50 / actual" },
                labels: { formatter: (value: number) => `${value}x` },
              },
              xaxis: { labels: { rotate: 0 } },
              tooltip: {
                y: {
                  formatter: (value: number, opts?: { dataPointIndex: number }) =>
                    `${value}x (n=${buckets[opts?.dataPointIndex ?? 0]?.sample_count ?? 0})`,
                },
              },
            }}
          />
        </div>
      )}
    </div>
  );
}
