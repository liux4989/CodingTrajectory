import * as React from "react";
import { motion } from "motion/react";
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
import { AnimatedNumber } from "@/components/animated-number";
import { popIn, staggerItem } from "@/lib/motion";

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
    <motion.div variants={staggerItem}>
    <Card className="@container/card metric-card min-w-0 gap-0 overflow-hidden">
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="font-display text-metric font-extrabold tabular-nums leading-tight">
          {typeof value === "number" ? (
            <AnimatedNumber value={value} />
          ) : (
            value
          )}
        </CardTitle>
        {trend ? (
          <CardAction>
            <motion.div variants={popIn}>
              <Badge variant="outline">
                {trend.direction === "down" ? "▼" : "▲"} {trend.value}
              </Badge>
            </motion.div>
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
            <motion.div
              className="h-full rounded-full bg-primary"
              initial={{ width: 0 }}
              animate={{ width: `${Math.min(100, Math.max(0, Math.round(ratio * 100)))}%` }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1], delay: 0.1 }}
            />
          </div>
        ) : null}
      </CardFooter>
    </Card>
    </motion.div>
  );
}
