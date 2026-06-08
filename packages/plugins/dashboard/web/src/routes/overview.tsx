import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchOverview } from "../api";
import { MetricSkeleton } from "../components/ui/skeleton";
import { RouteHeader } from "../components/route-header";
import { MetricCard } from "../components/metric-card";
import { RefreshButton } from "../components/refresh-button";
import { ReasonSummary } from "../components/badges";
import { StateBlock } from "../components/state-block";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";

export function OverviewRoute() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });

  if (overview.isPending) {
    return (
      <div className="route-stack">
        <RouteHeader eyebrow="Operational scan" title="Loading dashboard data" />
        <section className="metric-grid">
          {Array.from({ length: 4 }, (_, i) => <MetricSkeleton key={i} />)}
        </section>
      </div>
    );
  }

  if (overview.isError) return <StateBlock title="Dashboard unavailable" detail={overview.error.message} />;

  const data = overview.data;
  const vendorEntries = Object.entries(data.projects.vendors);
  const projectCount = data.projects.count || 1;
  const sessionCount = data.sessions.count || 1;

  return (
    <div className="route-stack">
      <RouteHeader
        eyebrow="Operational scan"
        title="A compact control room for projects, sessions, and safe cleanup."
        action={<RefreshButton queries={["overview"]} />}
      />
      <section className="metric-grid">
        <MetricCard
          label="Projects"
          value={data.projects.count}
          detail={`${vendorEntries.length} active vendor source(s)`}
          sparklineEntries={vendorEntries.map(([label, value]) => ({ label: label.slice(0, 3), value }))}
        />
        <MetricCard label="Recent sessions" value={data.sessions.count} detail="Default 30 day window" />
        <MetricCard
          label="Project cleanup candidates"
          value={data.cleanup.projects.candidate_count}
          detail={`${data.cleanup.projects.skipped_count} skipped`}
          ratio={data.cleanup.projects.candidate_count / projectCount}
        />
        <MetricCard
          label="Empty session candidates"
          value={data.cleanup.sessions.candidate_count}
          detail={`${data.cleanup.sessions.skipped_count} skipped`}
          ratio={data.cleanup.sessions.candidate_count / sessionCount}
        />
      </section>
      <section className="split-grid">
        <Card className="panel-surface">
          <CardHeader>
            <CardTitle>Vendor Coverage</CardTitle>
            <CardDescription>Project metadata grouped by agent vendor.</CardDescription>
          </CardHeader>
          <CardContent className="badge-cloud">
            {vendorEntries.length ? (
              vendorEntries.map(([vendor, count]) => (
                <Badge key={vendor}>
                  {vendor} <strong>{count}</strong>
                </Badge>
              ))
            ) : (
              <p className="muted">No vendor metadata found.</p>
            )}
          </CardContent>
        </Card>
        <Card className="panel-surface">
          <CardHeader>
            <CardTitle>Cleanup Posture</CardTitle>
            <CardDescription>Candidate counts are previews. Nothing moves until you confirm a selected action.</CardDescription>
          </CardHeader>
          <CardContent className="reason-list">
            <ReasonSummary title="Project skips" reasons={data.cleanup.projects.skipped_reasons} />
            <ReasonSummary title="Session skips" reasons={data.cleanup.sessions.skipped_reasons} />
          </CardContent>
        </Card>
      </section>
    </div>
  );
}
