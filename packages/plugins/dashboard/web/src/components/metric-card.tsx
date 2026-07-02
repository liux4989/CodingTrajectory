import * as React from "react";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { MiniBarChart } from "@/components/charts";

type MetricCardProps = {
  label: string;
  value: number | string;
  detail: string;
  sparklineEntries?: Array<{ label: string; value: number }>;
  ratio?: number;
  trend?: { value: string; direction: "up" | "down" };
};

/**
 * Metric card following the dashboard-01 SectionCards composition: a header
 * with description (label), title (value) and an optional trend badge in the
 * action slot, a mini bar chart in the content area, and a footer with the
 * detail line plus an optional ratio bar.
 */
export function MetricCard({ label, value, detail, sparklineEntries, ratio, trend }: MetricCardProps) {
  return (
    <Card className="@container/card metric-card min-w-0 gap-0 overflow-hidden">
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="font-display text-metric font-extrabold tabular-nums leading-tight">
          {typeof value === "number" ? value.toLocaleString() : value}
        </CardTitle>
        {trend ? (
          <CardAction>
            <Badge variant="outline">
              {trend.direction === "down" ? "▼" : "▲"} {trend.value}
            </Badge>
          </CardAction>
        ) : null}
      </CardHeader>
      {sparklineEntries?.length ? (
        <CardContent className="pb-0">
          <MiniBarChart
            data={sparklineEntries}
            ariaLabel={`${label} distribution`}
          />
        </CardContent>
      ) : null}
      <CardFooter className="mt-auto flex-col items-start gap-1.5 text-body-sm">
        <p className="m-0 break-words text-muted-foreground">{detail}</p>
        {ratio != null ? (
          <div
            className="h-1.5 w-full overflow-hidden rounded-full bg-foreground/8"
            role="img"
            aria-label={`${Math.round(ratio * 100)}%`}
          >
            <div
              className="h-full rounded-full bg-primary transition-[width] duration-400"
              style={{ width: `${Math.round(ratio * 100)}%` }}
            />
          </div>
        ) : null}
      </CardFooter>
    </Card>
  );
}
