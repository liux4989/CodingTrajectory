import * as React from "react";
import { Link, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchSessionGraph } from "@/api";
import { PageHeader } from "@/components/route-header";
import { shortSessionId } from "@/components/session-link";
import { SessionTabs } from "@/components/session-tabs";
import { ContextWindowPanel } from "@/routes/context-window";
import { SessionTimelinePanel } from "@/routes/session-timeline";

/**
 * Session scope of a graph: context pressure and recorded evidence for exactly
 * one session. The graph route above owns fork structure and orchestration.
 */
export function SessionDetailRoute() {
  const { rootId, sessionId } = useParams({ from: "/graphs/$rootId/sessions/$sessionId" });
  const { tab } = useSearch({ from: "/graphs/$rootId/sessions/$sessionId" });
  const graphQuery = useQuery({
    queryKey: ["session-graph", rootId],
    queryFn: () => fetchSessionGraph(rootId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  const node = graphQuery.data?.overview.sessions.find((entry) => entry.session_id === sessionId);
  const label = node?.title || node?.agent_name || `Session ${shortSessionId(sessionId)}`;
  const project = graphQuery.data?.overview.project;

  return (
    <div className="route-container-wide w-full min-w-0 pb-8">
      <PageHeader
        title={label}
        description={`${project ? `${project} · ` : ""}session ${shortSessionId(sessionId)} of graph ${shortSessionId(rootId)}`}
        actions={
          <Link
            to="/graphs/$rootId"
            params={{ rootId }}
            search={{ branch: undefined }}
            className="text-body-sm font-medium text-primary hover:underline"
          >
            Open graph
          </Link>
        }
      />

      <SessionTabs rootId={rootId} sessionId={sessionId} active={tab} />

      {/* Keyed so filters/selection reset when the route session changes. */}
      <React.Fragment key={sessionId}>
        {tab === "timeline" ? (
          <SessionTimelinePanel rootId={rootId} sessionId={sessionId} />
        ) : (
          <ContextWindowPanel rootId={rootId} sessionId={sessionId} />
        )}
      </React.Fragment>
    </div>
  );
}
