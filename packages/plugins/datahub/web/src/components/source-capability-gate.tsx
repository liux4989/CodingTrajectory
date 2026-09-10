import * as React from "react";
import { ExternalLink, HardDrive } from "lucide-react";
import { Link } from "@tanstack/react-router";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StateBlock } from "@/components/state-block";
import { LoadingState } from "@/components/loading-state";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";
import {
  currentAppPath,
  hasCapability,
  sourceUrl,
  type DatahubCapability,
} from "@/lib/datahub-source";

const CAPABILITY_LABELS: Record<DatahubCapability, string> = {
  sessions: "Sessions",
  graphs: "Graphs",
  "session-detail": "Session detail",
  today: "Today",
  compare: "Compare",
  "code-time": "Code Time",
};

export function SourceCapabilityGate({
  capability,
  children,
}: {
  capability: DatahubCapability;
  children: React.ReactNode;
}) {
  const delivery = useDatahubDelivery();
  if (delivery.isLoading && delivery.profile == null) {
    return <LoadingState title="Checking data source" detail="Loading Datahub capabilities." />;
  }
  if (delivery.profile == null) {
    return <StateBlock title="Data source unavailable" detail={delivery.error ?? "Datahub capabilities could not be loaded."} />;
  }
  if (hasCapability(delivery.profile, capability)) return children;

  const label = CAPABILITY_LABELS[capability];
  const localUrl = sourceUrl("local", currentAppPath());
  return (
    <Card className="mx-auto w-full max-w-2xl">
      <CardHeader>
        <Badge variant="secondary" className="w-fit">
          Shared workspace
        </Badge>
        <CardTitle>{label} is available locally</CardTitle>
        <CardDescription>
          The shared workspace provides published sessions and graphs. This analysis requires local source evidence.
        </CardDescription>
      </CardHeader>
      <CardContent className="text-body-sm text-muted-foreground">
        Your shared view will stay open if the local Datahub is not currently running.
      </CardContent>
      <CardFooter className="flex flex-wrap gap-2">
        <Button asChild>
          <a href={localUrl} target="_blank" rel="noreferrer">
            <HardDrive data-icon="inline-start" />
            Open {label} in Local
            <ExternalLink data-icon="inline-end" />
          </a>
        </Button>
        <Button variant="outline" asChild>
          <Link to="/sessions" search={{ projectName: undefined }}>
            Continue with Sessions
          </Link>
        </Button>
      </CardFooter>
    </Card>
  );
}
