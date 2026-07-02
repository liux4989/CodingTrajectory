import * as React from "react";
import { cn } from "@/lib/utils";
import { DATE_RANGE_PRESETS, useDateRange, type DateRange } from "@/hooks/use-date-range";

const LABELS: Record<DateRange, string> = {
  1: "24h",
  7: "7d",
  30: "30d",
  90: "90d",
};

type Props = {
  className?: string;
  label?: string;
};

export function DateRangeToggle({ className, label = "Date range" }: Props) {
  const { days, setDays } = useDateRange();
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        "inline-flex items-center gap-1 rounded-lg border border-border-soft bg-background/50 p-1 text-body-sm",
        className,
      )}
    >
      {DATE_RANGE_PRESETS.map((value) => {
        const active = value === days;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setDays(value)}
            className={cn(
              "rounded-md px-3 py-1.5 font-medium transition-colors",
              active
                ? "bg-primary text-primary-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {LABELS[value]}
          </button>
        );
      })}
    </div>
  );
}
