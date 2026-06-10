import type { ProjectSlice } from "@/api";

function formatDuration(seconds: number): string {
  if (!seconds) return "-";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${seconds}s`;
}

function formatTokens(tokens: number): string {
  if (!tokens) return "-";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`;
  return String(tokens);
}

function formatCost(cost: number | null): string {
  if (cost == null) return "-";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

type ProjectTableProps = {
  projects: ProjectSlice[];
};

export function ProjectTable({ projects }: ProjectTableProps) {
  if (!projects.length) {
    return (
      <p className="py-8 text-center text-caption text-muted-foreground">
        No sessions found.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-body-sm">
        <thead>
          <tr className="border-b border-border bg-table-head text-left text-eyebrow font-display uppercase tracking-wider text-muted-foreground">
            <th className="px-4 py-2">Project</th>
            <th className="px-4 py-2 text-right">Sessions</th>
            <th className="px-4 py-2 text-right">Coding Time</th>
            <th className="px-4 py-2 text-right">Wait Time</th>
            <th className="px-4 py-2 text-right">Turns</th>
            <th className="px-4 py-2 text-right">Tool Calls</th>
            <th className="px-4 py-2 text-right">Tokens</th>
            <th className="px-4 py-2 text-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {projects.map((p) => (
            <ProjectRow key={p.project_name} project={p} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProjectRow({ project }: { project: ProjectSlice }) {
  return (
    <>
      <tr className="border-b border-border-subtle hover:bg-accent/30">
        <td className="px-4 py-2 font-medium">{project.project_name}</td>
        <td className="px-4 py-2 text-right tabular-nums">{project.session_count}</td>
        <td className="px-4 py-2 text-right tabular-nums">{formatDuration(project.execution_seconds)}</td>
        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{formatDuration(project.wait_seconds)}</td>
        <td className="px-4 py-2 text-right tabular-nums">{project.turns}</td>
        <td className="px-4 py-2 text-right tabular-nums">{project.tool_calls}</td>
        <td className="px-4 py-2 text-right tabular-nums">{formatTokens(project.tokens.total_tokens)}</td>
        <td className="px-4 py-2 text-right tabular-nums">{formatCost(project.cost_usd)}</td>
      </tr>
      {project.sessions.map((s) => (
        <tr key={s.root_session_id} className="border-b border-border-subtle/50 text-caption text-muted-foreground">
          <td className="px-4 py-1.5 pl-8">
            <code className="text-xs">{s.root_session_id.slice(0, 8)}</code>
            <span className="ml-2 text-foreground">{s.title || "-"}</span>
          </td>
          <td className="px-4 py-1.5 text-right" />
          <td className="px-4 py-1.5 text-right tabular-nums">{formatDuration(s.execution_seconds)}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{formatDuration(s.wait_seconds)}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{s.turns}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{s.tool_calls}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{formatTokens(s.tokens.total_tokens)}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{formatCost(s.cost_usd)}</td>
        </tr>
      ))}
    </>
  );
}
