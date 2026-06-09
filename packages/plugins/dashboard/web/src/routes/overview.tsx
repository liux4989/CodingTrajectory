import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchOverview } from "@/api";
import { MetricSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { MetricCard } from "@/components/metric-card";
import { RefreshButton } from "@/components/refresh-button";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function OverviewRoute() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });

  if (overview.isPending) {
    return (
      <div className="mx-auto grid max-w-[96rem] gap-5">
        <RouteHeader eyebrow="Operational scan" title="Loading dashboard data" />
        <section className="grid grid-cols-4 gap-4 max-lg:grid-cols-1">
          {Array.from({ length: 4 }, (_, i) => <MetricSkeleton key={i} />)}
        </section>
      </div>
    );
  }

  if (overview.isError) return <StateBlock title="Dashboard unavailable" detail={overview.error.message} />;

  const data = overview.data;
  const vendorEntries = Object.entries(data.projects.vendors);

  return (
    <div className="mx-auto grid max-w-[96rem] gap-5">
      <RouteHeader
        eyebrow="Operational scan"
        title="A compact overview of discovered projects, recent sessions, and vendor coverage."
        action={<RefreshButton queries={["overview"]} />}
      />
      <section className="grid grid-cols-4 gap-4 max-lg:grid-cols-1">
        <MetricCard
          label="Projects"
          value={data.projects.count}
          detail={`${vendorEntries.length} active vendor source(s)`}
          sparklineEntries={vendorEntries.map(([label, value]) => ({ label: label.slice(0, 3), value }))}
        />
        <MetricCard label="Recent sessions" value={data.sessions.count} detail="Default 30 day window" />
      </section>
      <section>
        <Card>
          <CardHeader>
            <CardTitle className="font-display text-xl tracking-tight">Vendor Coverage</CardTitle>
            <CardDescription>Project metadata grouped by agent vendor.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {vendorEntries.length ? (
              vendorEntries.map(([vendor, count]) => (
                <Badge key={vendor}>
                  {vendor} <strong>{count}</strong>
                </Badge>
              ))
            ) : (
              <p className="text-muted-foreground">No vendor metadata found.</p>
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}
