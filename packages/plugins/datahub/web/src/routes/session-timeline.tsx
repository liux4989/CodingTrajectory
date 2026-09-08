import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { Cloud, HardDrive } from "lucide-react";

import { fetchSessionEvidenceTimeline } from "@/api";
import { LoadingState } from "@/components/loading-state";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import {
  EvidenceExplorer,
  type EvidenceFilterUpdate,
} from "@/components/session-timeline/evidence-explorer";
import { TimelineSummary } from "@/components/session-timeline/timeline-summary";
import { TurnWaterfall, type WaterfallTurn } from "@/components/session-timeline/turn-waterfall";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";

/**
 * Evidence timeline panel for the session scope: source-linked requests,
 * responses, tools, and failures in recorded order. Child-agent turns stay in
 * their own explicitly labeled sections; selecting one opens that session
 * instead of folding its evidence into this session.
 */
export function SessionTimelinePanel({ rootId, sessionId }: { rootId: string; sessionId: string }) {
  const search = useSearch({ from: "/graphs/$rootId/sessions/$sessionId" });
  const navigate = useNavigate({ from: "/graphs/$rootId/sessions/$sessionId" });
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
        search: (current) => ({ ...current, ...updates, tab: "timeline" }),
        replace: true,
      });
    },
    [navigate],
  );

  const selectTurn = React.useCallback(
    (turn: WaterfallTurn) => {
      if (turn.sessionId === sessionId) {
        updateSearch({ kind: undefined, artifact: undefined, vendor: undefined, outcome: undefined, entry: turn.entryId, turn: undefined });
        return;
      }
      // A turn from another session belongs to that session's scope.
      void navigate({
        to: "/graphs/$rootId/sessions/$sessionId",
        params: { rootId, sessionId: turn.sessionId },
        search: { tab: "timeline" },
      });
    },
    [navigate, rootId, sessionId, updateSearch],
  );

  if (query.isPending) {
    return (
      <div className="pt-4">
        <LoadingState title="Loading evidence timeline" detail="Reading retained canonical session activity." />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="pt-4">
        <StateBlock title="Evidence timeline failed" detail={query.error.message} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const payload = query.data;
  const sourceFailures = delivery.sourceStatus?.failed ?? 0;
  const incompleteSources = delivery.sourceStatus?.incomplete ?? 0;
  const lagSeconds = delivery.freshness?.lag_seconds;

  return (
    <div className="grid gap-4 pt-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="ml-auto flex items-center gap-3">
          <Badge
            variant="outline"
            className="gap-1.5"
            title={payload.transport
              ? `Workspace ${payload.transport.workspace_id} · authoritative ${payload.transport.content_scope} snapshot`
              : "Data materialized from sources on this machine"}
          >
            {payload.transport ? <Cloud aria-hidden="true" /> : <HardDrive aria-hidden="true" />}
            {payload.transport ? `Remote snapshot · ${payload.transport.snapshot_sequence}` : "Local sources"}
          </Badge>
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
        </div>
      </div>

      <TimelineSummary entries={payload.entries} warnings={payload.warnings} />

      <TurnWaterfall entries={payload.entries} sessionId={sessionId} onSelect={selectTurn} />

      <EvidenceExplorer
        entries={payload.entries}
        branches={payload.branches}
        state={{
          kind: search.kind ?? "all",
          artifact: search.artifact ?? "all",
          vendor: search.vendor ?? "all",
          outcome: search.outcome ?? "all",
          entry: search.entry,
          turn: search.turn,
        }}
        onChange={updateSearch}
      />
    </div>
  );
}
