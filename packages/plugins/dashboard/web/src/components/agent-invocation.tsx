import * as React from "react";
import { Loader2, Send, Square, Sparkles, X } from "lucide-react";
import { runAgentTurn, type AgentTurnResult, type JobRecord } from "@/api";
import { useJob } from "@/hooks/use-job";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatElapsed } from "@/hooks/use-elapsed-timer";
import { cn } from "@/lib/utils";

type AgentTurnOptions = {
  outputSchema?: Record<string, unknown> | null;
  newThread?: boolean;
};

type PendingTurn = {
  prompt: string;
  options: AgentTurnOptions;
};

export function useAgentTurn(initialThreadId: string | null = null) {
  const [threadId, setThreadId] = React.useState<string | null>(initialThreadId);
  const [pendingTurn, setPendingTurn] = React.useState<PendingTurn | null>(null);
  const job = useJob<AgentTurnResult>({
    start: () => {
      if (!pendingTurn) throw new Error("agent prompt is required");
      return runAgentTurn({
        prompt: pendingTurn.prompt,
        threadId: pendingTurn.options.newThread ? null : threadId,
        outputSchema: pendingTurn.options.outputSchema,
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
      setPendingTurn({ prompt: trimmed, options });
      job.reset();
    },
    [job],
  );

  React.useEffect(() => {
    if (pendingTurn && job.status === "idle") {
      job.start();
    }
  }, [job, pendingTurn]);

  const reset = React.useCallback(() => {
    setPendingTurn(null);
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

export function AgentTurnStatus({
  status,
  elapsedMs,
  progress,
  onCancel,
}: {
  status: ReturnType<typeof useAgentTurn>["status"];
  elapsedMs: number;
  progress: string | null;
  onCancel: () => void;
}) {
  if (status !== "pending" && status !== "running") return null;
  return (
    <div className="panel flex items-center gap-3">
      <Loader2 size={18} className="shrink-0 animate-spin text-primary" />
      <div className="min-w-0 flex-1">
        <p className="m-0 title-state">Running agent</p>
        <p className="m-0 text-body-sm text-muted-foreground">
          Codex is working in a page-lived app-server conversation.
        </p>
        {progress ? <p className="m-0 mt-1 text-caption text-muted-foreground">{progress}</p> : null}
      </div>
      {elapsedMs > 0 ? (
        <span className="shrink-0 mono text-caption text-muted-foreground">
          {formatElapsed(elapsedMs)}
        </span>
      ) : null}
      <Button size="icon-sm" variant="ghost" onClick={onCancel} aria-label="Cancel">
        <X size={15} />
      </Button>
    </div>
  );
}

export function AgentResponseBlock({
  result,
  className,
}: {
  result: AgentTurnResult | null;
  className?: string;
}) {
  if (!result) return null;
  return (
    <section className={cn("panel grid gap-3", className)} aria-label="Agent response">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">Codex</Badge>
        <span className="mono text-caption text-muted-foreground">
          thread {shortId(result.app_server_thread_id)}
        </span>
        {result.app_server_turn_id ? (
          <span className="mono text-caption text-muted-foreground">
            turn {shortId(result.app_server_turn_id)}
          </span>
        ) : null}
      </div>
      <div className="whitespace-pre-wrap break-words text-body-sm leading-relaxed">
        {result.response_text}
      </div>
    </section>
  );
}

export function AgentFollowUpForm({
  disabled,
  label = "Follow up",
  placeholder = "Send feedback or the next instruction",
  submitLabel = "Send",
  onSubmit,
}: {
  disabled?: boolean;
  label?: string;
  placeholder?: string;
  submitLabel?: string;
  onSubmit: (value: string) => void;
}) {
  const [value, setValue] = React.useState("");
  const canSubmit = !disabled && value.trim().length > 0;
  return (
    <form
      className="grid gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (!canSubmit) return;
        onSubmit(value.trim());
        setValue("");
      }}
    >
      <label className="eyebrow-soft text-muted-foreground" htmlFor="agent-follow-up">
        {label}
      </label>
      <textarea
        id="agent-follow-up"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className="min-h-24 resize-y rounded-md border border-input bg-background px-3 py-2 text-body-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
      />
      <div className="flex justify-end">
        <Button type="submit" size="sm" disabled={!canSubmit}>
          {disabled ? <Square size={13} className="fill-current" /> : <Send size={15} />}
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

export function AgentRunButton({
  running,
  hasResult,
  disabled,
  onClick,
}: {
  running: boolean;
  hasResult: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <Button type="button" size="sm" disabled={disabled || running} onClick={onClick}>
      {running ? <Square size={13} className="fill-current" /> : <Sparkles size={15} />}
      {hasResult ? "Start new analysis" : "Run agent"}
    </Button>
  );
}

function shortId(value: string) {
  return value.length > 10 ? value.slice(0, 8) : value;
}
