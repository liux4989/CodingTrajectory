import * as React from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Sparkline } from "@/components/sparkline";

type MetricCardProps = {
  label: string;
  value: number;
  detail: string;
  sparklineEntries?: Array<{ label: string; value: number }>;
  ratio?: number;
};

export function MetricCard({ label, value, detail, sparklineEntries, ratio }: MetricCardProps) {
  return (
    <Card className="metric-card grid gap-1 [&_[data-slot=card-content]]:grid [&_[data-slot=card-content]]:gap-1">
      <CardContent className="grid gap-1">
        <p className="m-0 text-muted-foreground">{label}</p>
        <p className="m-0 font-display text-metric font-extrabold leading-tight">{value.toLocaleString()}</p>
        <p className="m-0 text-muted-foreground">{detail}</p>
        {sparklineEntries?.length ? <Sparkline entries={sparklineEntries} /> : null}
        {ratio != null ? (
          <div className="h-1.5 overflow-hidden rounded-full bg-foreground/8" role="img" aria-label={`${Math.round(ratio * 100)}%`}>
            <div className="h-full rounded-full bg-primary transition-[width] duration-400" style={{ width: `${Math.round(ratio * 100)}%` }} />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
