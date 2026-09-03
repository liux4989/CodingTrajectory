import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import { fetchSessionEvidenceTimeline } from "@/api";
import { LoadingState } from "@/components/loading-state";
import { PageHeader } from "@/components/route-header";
import { SessionViewTabs } from "@/components/session-view-tabs";
import { StateBlock } from "@/components/state-block";
import {
  EvidenceExplorer,
  type EvidenceFilterUpdate,
} from "@/components/session-timeline/evidence-explorer";
import { TimelineSummary } from "@/components/session-timeline/timeline-summary";
import { TurnWaterfall, type WaterfallTurn } from "@/components/session-timeline/turn-waterfall";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";

export function SessionTimelineRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  const search = useSearch({ from: "/sessions/$sessionId" });
  const navigate = useNavigate({ from: "/sessions/$sessionId" });
  const delivery = useDatahubDelivery();
  const query = useQuery({
    queryKey: ["session-timeline", sessionId],
    queryFn: () => fetchSessionEvidenceTimeline(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  const updateSearch = React.useCallback(
    (updates: EvidenceFilterUpdate) => {
      void navigate({
        search: (current) => ({ ...current, ...updates, view: "timeline" }),
        replace: true,
      });
    },
    [navigate],
  );

  if (query.isPending) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <LoadingState title="Loading evidence timeline" detail="Reading retained canonical session activity." />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <StateBlock title="Evidence timeline failed" detail={query.error.message} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const payload = query.data;
  const sourceFailures = delivery.sourceStatus?.failed ?? 0;
  const incompleteSources = delivery.sourceStatus?.incomplete ?? 0;
  const lagSeconds = delivery.freshness?.lag_seconds;

  return (
    <div className="route-container-wide w-full min-w-0 pb-8">
      <PageHeader
        title="Evidence timeline"
        description="Source-linked requests, responses, tools, failures, and child-agent activity in recorded order"
        actions={
          <div className="text-right">
            <p className="m-0 mono text-heading font-bold leading-none text-moss">
              rev {payload.revision}
            </p>
            <p className="m-0 mt-1 text-caption text-muted-foreground">
              {lagSeconds == null ? "refresh lag unavailable" : `${Math.round(lagSeconds)}s refresh lag`}
              {sourceFailures + incompleteSources > 0 ? (
                <>
                  {" "}· {sourceFailures} failed · {incompleteSources} incomplete sources
                </>
              ) : null}
            </p>
          </div>
        }
      />

      <SessionViewTabs sessionId={sessionId} active="timeline" />

      <TimelineSummary entries={payload.entries} warnings={payload.warnings} />

      <TurnWaterfall
        entries={payload.entries}
        onSelect={(turn: WaterfallTurn) =>
          updateSearch({ kind: undefined, artifact: undefined, agent: turn.sessionId, outcome: undefined, entry: turn.entryId })
        }
      />

      <EvidenceExplorer
        entries={payload.entries}
        branches={payload.branches}
        state={{
          kind: search.kind ?? "all",
          artifact: search.artifact ?? "all",
          agent: search.agent ?? "all",
          outcome: search.outcome ?? "all",
          entry: search.entry,
        }}
        onChange={updateSearch}
      />
    </div>
  );
}
