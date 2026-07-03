import * as React from "react";
import { runAgentTurn, type AgentTurnResult, type JobRecord } from "@/api";
import { useJob } from "@/hooks/use-job";

type AgentTurnOptions = {
  outputSchema?: Record<string, unknown> | null;
  ephemeral?: boolean;
  newThread?: boolean;
};

type PendingTurn = {
  prompt: string;
  options: AgentTurnOptions;
};

export function useAgentTurn(initialThreadId: string | null = null) {
  const [threadId, setThreadId] = React.useState<string | null>(initialThreadId);
  const pendingTurnRef = React.useRef<PendingTurn | null>(null);
  const job = useJob<AgentTurnResult>({
    start: () => {
      const pendingTurn = pendingTurnRef.current;
      if (!pendingTurn) throw new Error("agent prompt is required");
      return runAgentTurn({
        prompt: pendingTurn.prompt,
        threadId: pendingTurn.options.newThread ? null : threadId,
        outputSchema: pendingTurn.options.outputSchema,
        ephemeral: pendingTurn.options.ephemeral,
      });
    },
    resolve: (record: JobRecord) => record.result as unknown as AgentTurnResult,
  });

  React.useEffect(() => {
    if (job.data?.app_server_thread_id) {
      setThreadId(job.data.app_server_thread_id);
    }
  }, [job.data?.app_server_thread_id]);

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
    setThreadId(initialThreadId);
    job.reset();
  }, [initialThreadId, job]);

  return {
    ...job,
    threadId,
    result: job.data,
    run,
    reset,
  };
}
