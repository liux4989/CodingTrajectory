import * as React from "react";
import { Card, CardContent } from "./ui/card";
import { Sparkline } from "./sparkline";

type MetricCardProps = {
  label: string;
  value: number;
  detail: string;
  sparklineEntries?: Array<{ label: string; value: number }>;
  ratio?: number;
};

export function MetricCard({ label, value, detail, sparklineEntries, ratio }: MetricCardProps) {
  return (
    <Card className="metric-card">
      <CardContent>
        <p className="metric-label">{label}</p>
        <p className="metric-value">{value.toLocaleString()}</p>
        <p className="metric-detail">{detail}</p>
        {sparklineEntries?.length ? <Sparkline entries={sparklineEntries} /> : null}
        {ratio != null ? (
          <div className="metric-bar-track" role="img" aria-label={`${Math.round(ratio * 100)}%`}>
            <div className="metric-bar-fill" style={{ inlineSize: `${Math.round(ratio * 100)}%` }} />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
