import type { ForecastKind } from "@/api";
import { cn } from "@/lib/utils";

const KIND_STYLES: Record<ForecastKind, { label: string; className: string }> = {
  historical_backcast: {
    label: "backcast",
    className: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  },
  prospective: {
    label: "prospective",
    className: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  },
  prospective_unbound: {
    label: "unbound",
    className: "bg-sky-500/15 text-sky-700 dark:text-sky-400",
  },
  runtime_advisory: {
    label: "advisory",
    className: "bg-violet-500/15 text-violet-700 dark:text-violet-400",
  },
};

/**
 * Every forecast artifact is labeled by kind: historical backcasts are not
 * prospective calibration evidence, and the UI must keep that visible.
 */
export function ForecastKindBadge({ kind, className }: { kind: ForecastKind; className?: string }) {
  const style = KIND_STYLES[kind] ?? {
    label: kind,
    className: "bg-muted text-muted-foreground",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-eyebrow font-display uppercase tracking-wider",
        style.className,
        className,
      )}
    >
      {style.label}
    </span>
  );
}
