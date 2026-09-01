import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchDatahubChanges,
  fetchDatahubSnapshot,
  type ContextWindowPayload,
  type DatahubChanges,
  type DatahubFreshness,
  type DatahubSnapshot,
  type DatahubSourceStatus,
} from "@/api";

const CHANGE_POLL_MS = 12_000;
const MAX_STREAM_RECONNECTS = 3;
const STREAM_RECONNECT_MS = 1_500;
const MAX_INCREMENTAL_ENTITIES = 250;

const QUERY_FAMILIES = {
  overview: [["overview"]],
  projects: [["projects"], ["project"]],
  // Context windows refetch via session-scoped invalidations/upserts instead —
  // mapping them here would reset every open window on any session change.
  sessions: [["sessions"]],
  "model-usage": [["model-usage"]],
  "token-efficiency": [["token-efficiency"]],
  "context-window": [["context-window"]],
  "session-timeline": [["session-timeline"]],
  "session-tree": [["session-tree"]],
  "session-graph": [["session-graph"]],
} as const;

const ALL_QUERY_FAMILIES = Object.values(QUERY_FAMILIES).flat();

export type DatahubDeliveryState = {
  mode: "live" | "reconnecting" | "polling";
  revision: number | null;
  generatedAt: string | null;
  freshness: DatahubFreshness | null;
  catchingUp: boolean;
  sourceStatus: DatahubSourceStatus | null;
  minimumAvailableRevision: number | null;
  isLoading: boolean;
  isRefreshing: boolean;
  error: string | null;
};

const DatahubDeliveryContext = React.createContext<DatahubDeliveryState | null>(null);

function familiesFor(name: string): readonly (readonly string[])[] {
  return QUERY_FAMILIES[name as keyof typeof QUERY_FAMILIES] ?? [];
}

// A context-window query is keyed by the URL session id, which may be any
// member of a session graph (e.g. a subagent). Reset it when the changed
// graph's membership intersects the key or the cached payload's sections.
function resetContextWindowQueries(
  queryClient: ReturnType<typeof useQueryClient>,
  graphSessionIds: ReadonlySet<string>,
) {
  for (const query of queryClient.getQueryCache().findAll({ queryKey: ["context-window"] })) {
    const sessionId = (query.queryKey as readonly unknown[])[1];
    if (typeof sessionId !== "string") continue;
    const data = query.state.data as ContextWindowPayload | undefined;
    const affected =
      graphSessionIds.has(sessionId) ||
      (data != null &&
        (graphSessionIds.has(data.session_id) ||
          graphSessionIds.has(data.active_session_id) ||
          data.session_sections.some((section) => graphSessionIds.has(section.session_id))));
    if (affected) {
      void queryClient.resetQueries({ queryKey: query.queryKey, exact: true });
    }
  }
}

function sessionIdsForSummary(payload: unknown, fallback: string): ReadonlySet<string> {
  const ids = (payload as { session_ids?: unknown } | null | undefined)?.session_ids;
  if (Array.isArray(ids) && ids.length > 0 && ids.every((id) => typeof id === "string")) {
    return new Set(ids as string[]);
  }
  return new Set([fallback]);
}

function statusFromSnapshot(snapshot: DatahubSnapshot | undefined): DatahubDeliveryState {
  return {
    mode: "reconnecting",
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
  changes: DatahubChanges,
) {
  const affected = new Map<string, readonly string[]>();
  const remember = (family: readonly string[]) => affected.set(family.join("/"), family);
  const totalEntities = changes.upserts.length + changes.deletions.length;

  if (totalEntities <= MAX_INCREMENTAL_ENTITIES) {
    for (const upsert of changes.upserts) {
      queryClient.setQueryData(
        ["datahub", "entities", upsert.entity_type, upsert.entity_id],
        upsert.payload,
      );
      if (upsert.entity_type === "sessions") {
        remember(["sessions"]);
        resetContextWindowQueries(queryClient, sessionIdsForSummary(upsert.payload, upsert.entity_id));
        continue;
      }
      for (const family of familiesFor(upsert.entity_type)) remember(family);
    }
    for (const deletion of changes.deletions) {
      queryClient.removeQueries({
        queryKey: ["datahub", "entities", deletion.entity_type, deletion.entity_id],
      });
      if (deletion.entity_type === "sessions") {
        remember(["sessions"]);
        resetContextWindowQueries(queryClient, new Set([deletion.entity_id]));
        continue;
      }
      for (const family of familiesFor(deletion.entity_type)) remember(family);
    }
  } else {
    for (const family of ALL_QUERY_FAMILIES) remember(family);
  }

  if (changes.invalidations.length <= MAX_INCREMENTAL_ENTITIES) {
    for (const raw of changes.invalidations) {
      // Scoped invalidations arrive as "family@scope"; a comma-joined root
      // scope on context-window targets only the windows for those graphs.
      const at = raw.indexOf("@");
      const family = at === -1 ? raw : raw.slice(0, at);
      const scope = at === -1 ? "" : raw.slice(at + 1);
      if (family === "context-window" && scope && !scope.startsWith("recent:")) {
        resetContextWindowQueries(queryClient, new Set(scope.split(",")));
        continue;
      }
      for (const queryFamily of familiesFor(family)) remember(queryFamily);
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

export function DatahubDeliveryProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const snapshot = useQuery({
    queryKey: ["datahub", "snapshot"],
    queryFn: ({ signal }) => fetchDatahubSnapshot(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const [delivery, setDelivery] = React.useState<DatahubDeliveryState>(() => statusFromSnapshot(undefined));
  const appliedRevision = React.useRef<number | null>(null);
  const targetRevision = React.useRef<number | null>(null);
  // The useQuery result object is not referentially stable; keep it in a ref
  // so effects can depend on the underlying data instead of the container.
  const snapshotRef = React.useRef(snapshot);
  snapshotRef.current = snapshot;

  React.useEffect(() => {
    if (!snapshot.data) return;
    appliedRevision.current = snapshot.data.revision;
    targetRevision.current = snapshot.data.revision;
    setDelivery(statusFromSnapshot(snapshot.data));
  }, [snapshot.data]);

  React.useEffect(() => {
    if (!snapshot.data) return;
    let disposed = false;
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let pollTimer: number | null = null;
    let reconnects = 0;
    let catchUpWorker: Promise<void> | null = null;
    const controller = new AbortController();

    const recover = () => {
      resetAllRouteQueries(queryClient);
      appliedRevision.current = null;
      targetRevision.current = null;
      void snapshotRef.current.refetch();
    };
    const catchUp = () => {
      if (catchUpWorker) return catchUpWorker;
      catchUpWorker = (async () => {
        while (!disposed && appliedRevision.current != null && targetRevision.current != null && appliedRevision.current < targetRevision.current) {
          const currentRevision = appliedRevision.current;
          const payload = await fetchDatahubChanges({ afterRevision: currentRevision, signal: controller.signal });
          if (payload.reset_required || payload.from_revision !== currentRevision) {
            recover();
            return;
          }
          if (payload.to_revision <= currentRevision) return;
          applyChanges(queryClient, payload);
          appliedRevision.current = payload.to_revision;
          setDelivery((current) => ({ ...current, revision: payload.to_revision, freshness: payload.freshness, catchingUp: payload.catching_up, sourceStatus: payload.source_status, isLoading: false, error: null }));
        }
      })().catch((error: unknown) => {
        if (!disposed && !(error instanceof DOMException && error.name === "AbortError")) {
          setDelivery((current) => ({ ...current, error: error instanceof Error ? error.message : String(error) }));
        }
      }).finally(() => { catchUpWorker = null; });
      return catchUpWorker;
    };
    const wake = (revision: number) => {
      if (!Number.isFinite(revision) || revision <= (appliedRevision.current ?? -1)) return;
      targetRevision.current = Math.max(targetRevision.current ?? revision, revision);
      void catchUp();
    };
    const poll = () => {
      if (!disposed && document.visibilityState === "visible" && appliedRevision.current != null) {
        targetRevision.current = Math.max(targetRevision.current ?? 0, appliedRevision.current + 1);
        void catchUp();
      }
    };
    const startPolling = () => {
      source?.close();
      setDelivery((current) => ({ ...current, mode: "polling" }));
      poll();
      pollTimer = window.setInterval(poll, CHANGE_POLL_MS);
    };
    const connect = () => {
      if (disposed) return;
      setDelivery((current) => ({ ...current, mode: reconnects ? "reconnecting" : current.mode }));
      source = new EventSource("/api/datahub/events");
      source.addEventListener("open", () => {
        reconnects = 0;
        setDelivery((current) => ({ ...current, mode: "live", error: null }));
      });
      source.addEventListener("revision", (event) => {
        try { wake(Number((JSON.parse((event as MessageEvent<string>).data) as { revision: number }).revision)); } catch { /* malformed wakeups are ignored */ }
      });
      source.onerror = () => {
        source?.close();
        if (disposed) return;
        reconnects += 1;
        if (reconnects > MAX_STREAM_RECONNECTS) startPolling();
        else {
          setDelivery((current) => ({ ...current, mode: "reconnecting" }));
          reconnectTimer = window.setTimeout(connect, STREAM_RECONNECT_MS * reconnects);
        }
      };
    };
    connect();
    return () => {
      disposed = true;
      controller.abort();
      source?.close();
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      if (pollTimer != null) window.clearInterval(pollTimer);
    };
  }, [snapshot.data, queryClient]);

  const error = snapshot.error;
  const errorMessage = error instanceof Error ? error.message : error ? String(error) : null;
  const value = React.useMemo<DatahubDeliveryState>(
    () => ({
      ...delivery,
      isLoading: snapshot.isLoading,
      // Background change detection should remain invisible. Only a snapshot
      // recovery is a user-visible refresh; a new revision updates route data.
      isRefreshing: snapshot.isRefetching,
      error: errorMessage,
    }),
    [delivery, errorMessage, snapshot.isLoading, snapshot.isRefetching],
  );

  return (
    <DatahubDeliveryContext.Provider value={value}>
      {children}
    </DatahubDeliveryContext.Provider>
  );
}

export function useDatahubDelivery() {
  const value = React.useContext(DatahubDeliveryContext);
  if (!value) throw new Error("useDatahubDelivery must be used inside DatahubDeliveryProvider");
  return value;
}
