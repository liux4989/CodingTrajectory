import * as React from "react";
import { Navigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchSessionGraph } from "@/api";
import { LoadingState } from "@/components/loading-state";
import { StateBlock } from "@/components/state-block";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";

/** Resolve a session's graph identity, then open the available scoped route. */
export function SessionResolverRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  const search = useSearch({ from: "/sessions/$sessionId" });
  const { profile, isLoading: isLoadingSource } = useDatahubDelivery();
  const query = useQuery({
    queryKey: ["session-graph", sessionId],
    queryFn: () => fetchSessionGraph(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  if (query.isPending || (isLoadingSource && profile == null)) {
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

  if (profile?.kind === "remote") {
    return (
      <Navigate
        to="/graphs/$rootId"
        params={{ rootId }}
        search={{ branch: sessionId === rootId ? undefined : sessionId }}
        replace
      />
    );
  }

  return (
    <Navigate
      to="/graphs/$rootId/sessions/$sessionId"
      params={{ rootId, sessionId }}
      search={{ tab: search.tab }}
      replace
    />
  );
}
