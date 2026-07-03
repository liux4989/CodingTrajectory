import * as React from "react";
import {
  closeAgentSession,
  createAgentSession,
  runAgentSessionTurn,
  type AgentTurnResult,
  type JobRecord,
} from "@/api";
import { useJob } from "@/hooks/use-job";

type AgentTurnOptions = {
  outputSchema?: Record<string, unknown> | null;
  ephemeral?: boolean;
  newSession?: boolean;
  newThread?: boolean;
};

type PendingTurn = {
  prompt: string;
  options: AgentTurnOptions;
};

type StoredAgentState = {
  agentSessionId: string | null;
  jobId: string | null;
};

export function useAgentTurn(routeScope = "dashboard-agent") {
  const storageKey = `ct-dashboard-agent:${routeScope}`;
  const initialState = React.useMemo(() => readStoredAgentState(storageKey), [storageKey]);
  const [agentSessionId, setAgentSessionId] = React.useState<string | null>(initialState.agentSessionId);
  const [initialJobId] = React.useState<string | null>(initialState.jobId);
  const agentSessionIdRef = React.useRef<string | null>(initialState.agentSessionId);
  const pendingTurnRef = React.useRef<PendingTurn | null>(null);

  const persist = React.useCallback(
    (next: Partial<StoredAgentState>) => {
      const current = readStoredAgentState(storageKey);
      const value = { ...current, ...next };
      writeStoredAgentState(storageKey, value);
    },
    [storageKey],
  );

  const updateAgentSessionId = React.useCallback(
    (value: string | null) => {
      agentSessionIdRef.current = value;
      setAgentSessionId(value);
      persist({ agentSessionId: value });
    },
    [persist],
  );

  const job = useJob<AgentTurnResult>({
    initialJobId,
    onJobId: (jobId) => persist({ jobId }),
    onTerminal: () => persist({ jobId: null }),
    start: async () => {
      const pendingTurn = pendingTurnRef.current;
      if (!pendingTurn) throw new Error("agent prompt is required");
      let sessionId = agentSessionIdRef.current;
      if (!sessionId || pendingTurn.options.newSession || pendingTurn.options.newThread) {
        if (sessionId) {
          closeAgentSession(sessionId).catch(() => {});
        }
        const session = await createAgentSession({
          routeScope,
          ephemeral: pendingTurn.options.ephemeral,
        });
        sessionId = session.agent_session_id;
        updateAgentSessionId(sessionId);
      }
      return runAgentSessionTurn({
        agentSessionId: sessionId,
        prompt: pendingTurn.prompt,
        outputSchema: pendingTurn.options.outputSchema,
      });
    },
    resolve: (record: JobRecord) => record.result as unknown as AgentTurnResult,
  });

  React.useEffect(() => {
    if (job.data?.agent_session_id) {
      updateAgentSessionId(job.data.agent_session_id);
    }
  }, [job.data?.agent_session_id, updateAgentSessionId]);

  const run = React.useCallback(
    (prompt: string, options: AgentTurnOptions = {}) => {
      const trimmed = prompt.trim();
      if (!trimmed) return;
      pendingTurnRef.current = { prompt: trimmed, options };
      job.reset();
      job.start();
    },
    [job],
  );

  const reset = React.useCallback(() => {
    pendingTurnRef.current = null;
    if (agentSessionIdRef.current) {
      closeAgentSession(agentSessionIdRef.current).catch(() => {});
    }
    updateAgentSessionId(null);
    persist({ jobId: null });
    job.reset();
  }, [job, persist, updateAgentSessionId]);

  return {
    ...job,
    agentSessionId,
    result: job.data,
    run,
    reset,
  };
}

function readStoredAgentState(storageKey: string): StoredAgentState {
  if (typeof window === "undefined") return { agentSessionId: null, jobId: null };
  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) return { agentSessionId: null, jobId: null };
    const parsed = JSON.parse(raw) as Partial<StoredAgentState>;
    return {
      agentSessionId: typeof parsed.agentSessionId === "string" ? parsed.agentSessionId : null,
      jobId: typeof parsed.jobId === "string" ? parsed.jobId : null,
    };
  } catch {
    return { agentSessionId: null, jobId: null };
  }
}

function writeStoredAgentState(storageKey: string, value: StoredAgentState) {
  if (typeof window === "undefined") return;
  if (!value.agentSessionId && !value.jobId) {
    window.sessionStorage.removeItem(storageKey);
    return;
  }
  window.sessionStorage.setItem(storageKey, JSON.stringify(value));
}
