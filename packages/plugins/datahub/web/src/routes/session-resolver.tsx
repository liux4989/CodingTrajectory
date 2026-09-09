import * as React from "react";
import { Navigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchSessionGraph } from "@/api";
import { LoadingState } from "@/components/loading-state";
import { StateBlock } from "@/components/state-block";
import { HOSTED_MODE } from "@/hosted/mode";

/**
 * Canonical "open this session" entry: resolves the session's graph identity
 * (its root session id), then redirects to the matching scoped route. Legacy
 * `?view=` values map onto the two scopes: graph/tree to the graph page,
 * context/timeline to the session page.
 */
export function SessionResolverRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  const search = useSearch({ from: "/sessions/$sessionId" });
  const query = useQuery({
    queryKey: ["session-graph", sessionId],
    queryFn: () => fetchSessionGraph(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  if (query.isPending) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <LoadingState title="Opening session" detail="Resolving the session's graph." />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <StateBlock title="Session lookup failed" detail={query.error.message} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const rootId = query.data.root_session_id || sessionId;

  if (HOSTED_MODE) {
    return (
      <Navigate
        to="/graphs/$rootId"
        params={{ rootId }}
        search={{ branch: sessionId === rootId ? undefined : sessionId }}
        replace
      />
    );
  }

  if (search.view === "timeline" || search.view === "context") {
    return (
      <Navigate
        to="/graphs/$rootId/sessions/$sessionId"
        params={{ rootId, sessionId }}
        search={{
          tab: search.view,
          kind: search.kind,
          artifact: search.artifact,
          vendor: search.vendor,
          outcome: search.outcome,
          entry: search.entry,
        }}
        replace
      />
    );
  }

  return (
    <Navigate
      to="/graphs/$rootId"
      params={{ rootId }}
      search={{ branch: search.view === "tree" ? sessionId : undefined }}
      replace
    />
  );
}
