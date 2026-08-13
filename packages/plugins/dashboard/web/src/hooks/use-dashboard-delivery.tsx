import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchDashboardChanges,
  fetchDashboardSnapshot,
  type DashboardChanges,
  type DashboardFreshness,
  type DashboardSnapshot,
  type DashboardSourceStatus,
} from "@/api";

const CHANGE_POLL_MS = 12_000;
const MAX_INCREMENTAL_ENTITIES = 250;

const QUERY_FAMILIES = {
  overview: [["overview"]],
  projects: [["projects"], ["project"]],
  sessions: [["sessions"], ["context-window"]],
  "model-usage": [["model-usage"]],
  "token-efficiency": [["token-efficiency"]],
  "context-window": [["context-window"]],
  "cache-breaks": [["cache-breaks"]],
} as const;

const ALL_QUERY_FAMILIES = Object.values(QUERY_FAMILIES).flat();

export type DashboardDeliveryState = {
  revision: number | null;
  generatedAt: string | null;
  freshness: DashboardFreshness | null;
  catchingUp: boolean;
  sourceStatus: DashboardSourceStatus | null;
  minimumAvailableRevision: number | null;
  isLoading: boolean;
  isRefreshing: boolean;
  error: string | null;
};

const DashboardDeliveryContext = React.createContext<DashboardDeliveryState | null>(null);

function familiesFor(name: string): readonly (readonly string[])[] {
  return QUERY_FAMILIES[name as keyof typeof QUERY_FAMILIES] ?? [];
}

function statusFromSnapshot(snapshot: DashboardSnapshot | undefined): DashboardDeliveryState {
  return {
    revision: snapshot?.revision ?? null,
    generatedAt: snapshot?.generated_at ?? null,
    freshness: snapshot?.freshness ?? null,
    catchingUp: snapshot?.catching_up ?? false,
    sourceStatus: snapshot?.source_status ?? null,
    minimumAvailableRevision: snapshot?.minimum_available_revision ?? null,
    isLoading: !snapshot,
    isRefreshing: false,
    error: null,
  };
}

function applyChanges(
  queryClient: ReturnType<typeof useQueryClient>,
  changes: DashboardChanges,
) {
  const affected = new Map<string, readonly string[]>();
  const remember = (family: readonly string[]) => affected.set(family.join("/"), family);
  const totalEntities = changes.upserts.length + changes.deletions.length;

  if (totalEntities <= MAX_INCREMENTAL_ENTITIES) {
    for (const upsert of changes.upserts) {
      queryClient.setQueryData(
        ["dashboard", "entities", upsert.entity_type, upsert.entity_id],
        upsert.payload,
      );
      for (const family of familiesFor(upsert.entity_type)) remember(family);
    }
    for (const deletion of changes.deletions) {
      queryClient.removeQueries({
        queryKey: ["dashboard", "entities", deletion.entity_type, deletion.entity_id],
      });
      for (const family of familiesFor(deletion.entity_type)) remember(family);
    }
  } else {
    for (const family of ALL_QUERY_FAMILIES) remember(family);
  }

  if (changes.invalidations.length <= MAX_INCREMENTAL_ENTITIES) {
    for (const invalidation of changes.invalidations) {
      for (const family of familiesFor(invalidation)) remember(family);
    }
  } else {
    for (const family of ALL_QUERY_FAMILIES) remember(family);
  }

  for (const queryKey of affected.values()) {
    // Cursor pages are bound to their snapshot revision. Resetting drops old
    // page params before active queries refetch, so a new first page can never
    // be combined with continuation cursors from an earlier revision.
    void queryClient.resetQueries({ queryKey, exact: false });
  }
}

function resetAllRouteQueries(queryClient: ReturnType<typeof useQueryClient>) {
  for (const queryKey of ALL_QUERY_FAMILIES) {
    void queryClient.resetQueries({ queryKey, exact: false });
  }
}

export function DashboardDeliveryProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const snapshot = useQuery({
    queryKey: ["dashboard", "snapshot"],
    queryFn: ({ signal }) => fetchDashboardSnapshot(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const [revision, setRevision] = React.useState<number | null>(null);
  const [delivery, setDelivery] = React.useState<DashboardDeliveryState>(() => statusFromSnapshot(undefined));
  const appliedRevision = React.useRef<number | null>(null);

  React.useEffect(() => {
    if (!snapshot.data) return;
    appliedRevision.current = snapshot.data.revision;
    setRevision(snapshot.data.revision);
    setDelivery(statusFromSnapshot(snapshot.data));
  }, [snapshot.data]);

  const changes = useQuery({
    queryKey: ["dashboard", "changes", revision],
    queryFn: ({ signal }) => fetchDashboardChanges({ afterRevision: revision ?? 0, signal }),
    enabled: revision != null,
    refetchInterval: () => (document.visibilityState === "visible" ? CHANGE_POLL_MS : false),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: 1,
  });

  React.useEffect(() => {
    const payload = changes.data;
    const currentRevision = appliedRevision.current;
    if (!payload || currentRevision == null) return;

    if (payload.reset_required || payload.from_revision !== currentRevision) {
      resetAllRouteQueries(queryClient);
      appliedRevision.current = null;
      setRevision(null);
      void snapshot.refetch();
      return;
    }

    if (payload.to_revision <= currentRevision) {
      setDelivery((current) => ({
        ...current,
        freshness: payload.freshness,
        catchingUp: payload.catching_up,
        sourceStatus: payload.source_status,
        isLoading: false,
        isRefreshing: false,
        error: null,
      }));
      return;
    }

    applyChanges(queryClient, payload);
    appliedRevision.current = payload.to_revision;
    setRevision(payload.to_revision);
    setDelivery((current) => ({
      ...current,
      revision: payload.to_revision,
      freshness: payload.freshness,
      catchingUp: payload.catching_up,
      sourceStatus: payload.source_status,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }));
  }, [changes.data, queryClient, snapshot]);

  const error = snapshot.error ?? changes.error;
  const value: DashboardDeliveryState = {
    ...delivery,
    isLoading: snapshot.isLoading,
    isRefreshing: snapshot.isRefetching || changes.isFetching,
    error: error instanceof Error ? error.message : error ? String(error) : null,
  };

  return (
    <DashboardDeliveryContext.Provider value={value}>
      {children}
    </DashboardDeliveryContext.Provider>
  );
}

export function useDashboardDelivery() {
  const value = React.useContext(DashboardDeliveryContext);
  if (!value) throw new Error("useDashboardDelivery must be used inside DashboardDeliveryProvider");
  return value;
}
