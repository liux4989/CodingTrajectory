import type { ComparisonRow, SessionRow } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDuration, formatMetricValue } from "@/lib/format";

export function ComparisonTable({ rows }: { rows: ComparisonRow[] }) {
  return (
    <Card className="content-section min-w-0">
      <CardHeader>
        <CardTitle>Model comparison</CardTitle>
        <CardDescription>Provider and model attribution from canonical model-usage rows.</CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length ? <Table>
          <TableHeader><TableRow><TableHead>Provider / model</TableHead><TableHead>Graphs</TableHead><TableHead>Tokens</TableHead><TableHead>Tok/s</TableHead><TableHead>Cache</TableHead><TableHead>Cost</TableHead><TableHead>Pricing</TableHead></TableRow></TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.key}>
                <TableCell className="max-w-72 truncate font-medium" title={row.label}>{row.label}</TableCell>
                <TableCell>{row.graphs.toLocaleString()}</TableCell>
                <TableCell>{formatMetricValue(row.processed_tokens, "tokens")}</TableCell>
                <TableCell>{formatMetricValue(row.processed_tokens_per_second, "rate")}</TableCell>
                <TableCell>{formatMetricValue(row.cache_hit_rate, "percent")}</TableCell>
                <TableCell>{formatMetricValue(row.cost_usd, "usd")}</TableCell>
                <TableCell>{row.pricing_coverage}/{row.graphs}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table> : <Empty><EmptyHeader><EmptyTitle>No model rows</EmptyTitle><EmptyDescription>No provider/model attribution is available for this cohort.</EmptyDescription></EmptyHeader></Empty>}
      </CardContent>
    </Card>
  );
}

export function SessionTable({ rows }: { rows: SessionRow[] }) {
  return (
    <Card className="content-section min-w-0">
      <CardHeader>
        <CardTitle>Session graph drill-down</CardTitle>
        <CardDescription>Bounded graph rows ranked by the active category metric.</CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length ? <Table>
          <TableHeader><TableRow><TableHead>Session graph</TableHead><TableHead>Model</TableHead><TableHead>Tokens</TableHead><TableHead>Tok/s</TableHead><TableHead>Cost evidence</TableHead><TableHead>Active</TableHead><TableHead>Wait</TableHead><TableHead>Turns</TableHead></TableRow></TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.session_graph_id}>
                <TableCell className="max-w-80"><span className="block truncate font-medium" title={row.title ?? row.session_graph_id}>{row.title ?? row.session_graph_id}</span><span className="block truncate font-mono text-xs text-muted-foreground">{row.project ?? "Unknown project"}</span></TableCell>
                <TableCell>{row.mixed_models ? <Badge variant="secondary">Mixed models</Badge> : row.model_label}</TableCell>
                <TableCell>{formatMetricValue(row.processed_tokens, "tokens")}</TableCell>
                <TableCell>{formatMetricValue(row.processed_tokens_per_second, "rate")}</TableCell>
                <TableCell>{row.cost_usd == null ? "Unavailable" : <span className="flex flex-col gap-1"><span>{formatMetricValue(row.cost_usd, "usd")}</span><Badge variant="outline">{row.cost_confidence ?? "unknown"}</Badge></span>}</TableCell>
                <TableCell>{row.active_seconds == null ? "Unavailable" : formatDuration(row.active_seconds)}</TableCell>
                <TableCell>{row.wait_seconds == null ? "Unavailable" : formatDuration(row.wait_seconds)}</TableCell>
                <TableCell>{row.turns.toLocaleString()}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table> : <Empty><EmptyHeader><EmptyTitle>No session graphs</EmptyTitle><EmptyDescription>No session graphs match the current cohort.</EmptyDescription></EmptyHeader></Empty>}
      </CardContent>
    </Card>
  );
}
