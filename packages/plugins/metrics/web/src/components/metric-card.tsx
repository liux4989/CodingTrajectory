import type { Highlight } from "@/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMetricValue } from "@/lib/format";

export function MetricCard({ highlight }: { highlight: Highlight }) {
  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardDescription>{highlight.label}</CardDescription>
        <CardTitle className="text-3xl tabular-nums" title={highlight.value == null ? undefined : String(highlight.value)}>{formatMetricValue(highlight.value, highlight.format)}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="m-0 text-sm text-muted-foreground">{highlight.detail}</p>
      </CardContent>
    </Card>
  );
}
