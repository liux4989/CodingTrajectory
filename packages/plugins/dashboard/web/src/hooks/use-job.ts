import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJobStatus, type JobRecord, type JobStatus } from "@/api";
import { useElapsedTimer } from "@/hooks/use-elapsed-timer";

export type UseJobStatus = "idle" | JobStatus;

export type UseJobOptions<T> = {
  /** Starts the job and returns its job id. */
  start: () => Promise<{ job_id: string }>;
  /** Extracts the typed payload from the job record's `result` once ready. */
  resolve: (record: JobRecord) => T;
  initialJobId?: string | null;
  onJobId?: (jobId: string | null) => void;
  onData?: (data: T) => void;
  onTerminal?: () => void;
  /** Polling interval in ms. Defaults to 1200. */
  intervalMs?: number;
};

export type UseJobResult<T> = {
  status: UseJobStatus;
  jobId: string | null;
  data: T | null;
  error: string | null;
  progress: string | null;
  elapsedMs: number;
  start: () => void;
  cancel: () => void;
  reset: () => void;
};

const DEFAULT_INTERVAL = 1200;

/**
 * Generic async-job hook built on TanStack Query.
 *
 * - `useMutation` submits the job and captures the `job_id`.
 * - `useQuery` polls `GET /api/jobs/<id>` with a dynamic `refetchInterval`
 *   that turns itself off once the job reaches `ready` or `error`.
 *
 * This reuses the project's existing `QueryClient` so the job query benefits
 * from request dedup, retry/backoff, cache, devtools, and `useIsFetching`
 * (the RefreshButton spinner already reacts to in-flight job polls).
 */
export function useJob<T>(options: UseJobOptions<T>): UseJobResult<T> {
  const {
    start: startJob,
    resolve,
    initialJobId = null,
    onJobId,
    onData,
    onTerminal,
    intervalMs = DEFAULT_INTERVAL,
  } = options;
  const queryClient = useQueryClient();
  const [jobId, setJobId] = React.useState<string | null>(initialJobId);

  React.useEffect(() => {
    setJobId(initialJobId);
  }, [initialJobId]);

  const startMutation = useMutation({
    mutationFn: startJob,
    onSuccess: (accepted) => {
      setJobId(accepted.job_id);
      onJobId?.(accepted.job_id);
    },
  });

  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => fetchJobStatus(jobId as string),
    enabled: jobId != null,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data || data.status === "ready" || data.status === "error") return false;
      return intervalMs;
    },
    retry: false,
  });

  const record = jobQuery.data ?? null;
  const jobQueryError =
    jobQuery.error instanceof Error
      ? jobQuery.error.message
      : jobQuery.error ? String(jobQuery.error) : null;
  const staleRecoveryJob = jobQuery.isError && jobQueryError === "unknown job_id";
  const status: UseJobStatus =
    jobId == null || staleRecoveryJob
      ? "idle"
      : jobQuery.isError
        ? "error"
        : (record?.status ?? "pending");
  const active = status === "pending" || status === "running";
  const elapsedMs = useElapsedTimer(active);

  const data =
    record?.status === "ready" && record.result ? resolve(record) : null;
  const error =
    record?.status === "error"
      ? record.error
      : jobQuery.isError && !staleRecoveryJob
        ? jobQueryError
      : startMutation.error instanceof Error
        ? startMutation.error.message
        : startMutation.error ? String(startMutation.error) : null;
  const progress = record?.progress ?? null;

  React.useEffect(() => {
    if (data) onData?.(data);
  }, [data, onData]);

  React.useEffect(() => {
    if (
      record?.status === "ready" ||
      record?.status === "error" ||
      (jobQuery.isError && !staleRecoveryJob)
    ) {
      onTerminal?.();
    }
  }, [jobQuery.isError, record?.status, onTerminal, staleRecoveryJob]);

  const start = React.useCallback(() => {
    setJobId(null);
    onJobId?.(null);
    startMutation.mutate();
  }, [onJobId, startMutation]);

  const clearJob = React.useCallback(() => {
    setJobId((current) => {
      if (current) queryClient.removeQueries({ queryKey: ["job", current] });
      onJobId?.(null);
      return null;
    });
  }, [onJobId, queryClient]);

  React.useEffect(() => {
    if (staleRecoveryJob) {
      clearJob();
    }
  }, [clearJob, staleRecoveryJob]);

  const cancel = React.useCallback(() => {
    startMutation.reset();
    clearJob();
  }, [clearJob, startMutation]);

  const reset = React.useCallback(() => {
    startMutation.reset();
    clearJob();
  }, [clearJob, startMutation]);

  return { status, jobId, data, error, progress, elapsedMs, start, cancel, reset };
}
