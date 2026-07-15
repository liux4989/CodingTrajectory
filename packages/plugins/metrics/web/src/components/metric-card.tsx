import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type MetricCardProps = {
  label: string;
  detail: string;
};

export function MetricCard({ label, detail }: MetricCardProps) {
  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-3xl tabular-nums">—</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="m-0 text-sm text-muted-foreground">{detail}</p>
      </CardContent>
    </Card>
  );
}
