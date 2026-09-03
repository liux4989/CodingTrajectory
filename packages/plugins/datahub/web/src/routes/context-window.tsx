import * as React from "react";
import { useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchContextWindow, type ContextWindowPayload } from "@/api";
import { formatCostUsd } from "@/lib/cache-breaks";
import { LoadingState } from "@/components/loading-state";
import { PageHeader } from "@/components/route-header";
import { SessionViewTabs } from "@/components/session-view-tabs";
import { StateBlock } from "@/components/state-block";
import { ContextEventExplorer } from "@/components/context-window/context-event-explorer";
import { ContextMaintenanceDetails } from "@/components/context-window/context-maintenance-details";
import { ContextWindowSummary } from "@/components/context-window/context-window-summary";
import { isEstimatedConfidence } from "@/components/context-window/shared";

export function ContextWindowRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
    gcTime: 60_000,
  });

  if (query.isPending) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <LoadingState title="Loading context window" detail="Reading normalized session projections." />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <StateBlock title="Context window failed" detail={query.error.message} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const payload = query.data;

  return (
    <div className="route-container-wide w-full min-w-0 pb-8">
      <PageHeader
        title="Context window"
        description="How full the window is, what consumes it, and where pressure builds"
        actions={<ModelEvidenceStatus payload={payload} />}
      />

      <SessionViewTabs sessionId={sessionId} active="context" />

      {/* Keyed so filters/selection reset when the route session changes. */}
      <React.Fragment key={sessionId}>
        <ContextWindowSummary payload={payload} />
        <ContextMaintenanceDetails payload={payload} />
        <ContextEventExplorer payload={payload} />
      </React.Fragment>
    </div>
  );
}

function ModelEvidenceStatus({ payload }: { payload: ContextWindowPayload }) {
  const usedEstimated = isEstimatedConfidence(payload.used_tokens?.confidence);
  return (
    <div className="text-right">
      <p className="m-0 mono text-heading font-bold leading-none text-moss">
        {payload.model ?? "model unavailable"}
      </p>
      <p className="m-0 mt-1 text-caption text-muted-foreground">
        {payload.vendor.replaceAll("_", " ")}
        {payload.token_cost ? (
          <>
            {" "}· <span className="mono">{formatCostUsd(payload.token_cost.value_usd)}</span>{" "}
            {payload.token_cost.confidence}
          </>
        ) : null}
        {payload.used_tokens ? ` · tokens ${usedEstimated ? "estimated" : "exact"}` : ""}
      </p>
    </div>
  );
}
