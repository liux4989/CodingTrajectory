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
  footer?: React.ReactNode;
  trend?: { value: string; direction: "up" | "down" };
};

/**
 * Metric card following the dashboard-01 SectionCards composition: a header
 * with description (label), title (value) and an optional trend badge in the
 * action slot, a mini bar chart in the content area, and a footer with the
 * detail line plus an optional ratio bar.
 */
export function MetricCard({ label, value, detail, footer, trend }: MetricCardProps) {
  return (
    <motion.div variants={staggerItem}>
    <Card className="@container/card metric-card min-w-0 gap-2 overflow-hidden p-3">
      <CardHeader className="p-0">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="font-display text-metric font-semibold tabular-nums leading-tight tracking-tight">
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
      <CardFooter className="mt-auto flex-col items-start gap-1.5 p-0 text-body-sm">
        <p className="m-0 break-words text-muted-foreground">{detail}</p>
        {footer}
      </CardFooter>
    </Card>
    </motion.div>
  );
}

MetricCard.Footer = function MetricCardFooter({ entries, label }: { entries: Array<{ label: string; value: number }>; label: string }) {
  return entries.length ? <CardContent className="w-full p-0"><MiniBarChart data={entries} ariaLabel={label} /></CardContent> : null;
};
