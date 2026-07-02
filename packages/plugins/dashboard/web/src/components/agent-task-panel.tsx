import * as React from "react";
import { Bot, ChevronDown, ChevronRight, Sparkles, Square } from "lucide-react";
import { runAgentTask, type AgentTaskResult, type AnalysisProvider, type JobRecord } from "@/api";
import { useJob } from "@/hooks/use-job";
import { LoadingState } from "@/components/loading-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";

type AgentTaskPanelProps = {
  title: string;
  description?: string;
  taskGoal: string;
  taskContext: string;
  defaultProvider?: AnalysisProvider;
  className?: string;
};

export function AgentTaskPanel({
  title,
  description,
  taskGoal,
  taskContext,
  defaultProvider = "codex",
  className,
}: AgentTaskPanelProps) {
  const [provider, setProvider] = React.useState<AnalysisProvider>(defaultProvider);
  const [showContext, setShowContext] = React.useState(false);
  const job = useJob<AgentTaskResult>({
    start: () => runAgentTask({ taskGoal, taskContext, provider }),
    resolve: (record: JobRecord) => record.result as unknown as AgentTaskResult,
  });
  const result = job.data;
  const running = job.status === "pending" || job.status === "running";
  const disabled = running || !taskGoal.trim() || !taskContext.trim();

  return (
    <Card className={cn("min-w-0", className)}>
      <CardHeader className="items-start gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0">
          <CardTitle className="flex min-w-0 items-center gap-2 font-display text-xl tracking-tight">
            <Bot size={18} className="shrink-0" />
            <span className="truncate">{title}</span>
          </CardTitle>
          {description ? <CardDescription className="break-words">{description}</CardDescription> : null}
        </div>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            job.reset();
            job.start();
          }}
        >
          <label className="sr-only" htmlFor="agent-task-provider">
            Agent provider
          </label>
          <Select
            value={provider}
            onValueChange={(value) => setProvider(value as AnalysisProvider)}
            disabled={running}
          >
            <SelectTrigger id="agent-task-provider" className="h-8 w-[8rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="codex">Codex</SelectItem>
              <SelectItem value="pi">Pi</SelectItem>
            </SelectContent>
          </Select>
          <Button type="submit" size="sm" disabled={disabled}>
            {running ? <Square size={13} className="fill-current" /> : <Sparkles size={15} />}
            {result ? "Rerun agent" : "Run agent"}
          </Button>
        </form>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="rounded-lg border border-border-soft bg-muted/20 p-3">
          <div className="eyebrow-soft text-muted-foreground">Goal</div>
          <p className="m-0 mt-1 break-words text-body-sm">{taskGoal}</p>
        </div>
        <button
          type="button"
          className="flex min-h-9 w-full items-center justify-between gap-2 rounded-md border border-border-soft px-3 py-2 text-left text-body-sm hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-expanded={showContext}
          onClick={() => setShowContext((current) => !current)}
        >
          <span className="font-medium">Task context</span>
          <span className="flex items-center gap-2 text-caption text-muted-foreground">
            {taskContext.length.toLocaleString()} chars
            {showContext ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          </span>
        </button>
        {showContext ? (
          <pre className="max-h-72 overflow-auto rounded-lg border border-border-soft bg-background p-3 text-caption leading-relaxed text-muted-foreground whitespace-pre-wrap">
            {taskContext}
          </pre>
        ) : null}
        {running ? (
          <LoadingState
            title="Running agent"
            detail="The coding agent is working on the task."
            elapsedMs={job.elapsedMs}
            progress={job.progress}
            onCancel={job.cancel}
          />
        ) : null}
        {job.status === "error" ? (
          <div role="alert" className="rounded-lg border border-destructive/35 bg-destructive/8 p-3 text-body-sm text-destructive">
            {job.error}
          </div>
        ) : null}
        {result ? (
          <section className="grid gap-3 rounded-lg border border-border-soft p-3" aria-label="Agent response">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{result.provider}</Badge>
              <span className="font-mono text-caption text-muted-foreground">{result.source}</span>
            </div>
            <div className="whitespace-pre-wrap break-words text-body-sm leading-relaxed">{result.response_text}</div>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}
