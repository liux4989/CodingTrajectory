import * as React from "react";
import type { CodeTimeProject } from "@/api";
import { formatCompactNumber, formatCostUsd, formatDuration } from "@/lib/format";
import { ProjectLink } from "@/components/project-link";
import { SessionLink } from "@/components/session-link";
import { ResponsiveDataList } from "@/components/responsive-data-list";

type CodeTimeProjectTableProps = {
  projects: CodeTimeProject[];
};

function durationOrDash(seconds: number): string {
  return seconds > 0 ? formatDuration(seconds) : "-";
}

export function CodeTimeProjectTable({ projects }: CodeTimeProjectTableProps) {
  if (!projects.length) {
    return (
      <p className="py-8 text-center text-caption text-muted-foreground">
        No sessions found for this window.
      </p>
    );
  }

  return (
    <ResponsiveDataList table={<div className="overflow-x-auto">
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
          {projects.map((project) => (
            <ProjectRow key={project.project_name} project={project} />
          ))}
        </tbody>
      </table>
    </div>} cards={projects.map((project) => (
      <article key={project.project_name} className="panel grid gap-2 bg-card">
        <div className="flex items-center justify-between gap-2"><ProjectLink name={project.project_name} /><span className="text-caption text-muted-foreground">{project.session_count} sessions</span></div>
        <div className="grid grid-cols-2 gap-2 text-body-sm"><span><b>{durationOrDash(project.execution_seconds)}</b><small className="block text-muted-foreground">Coding time</small></span><span><b>{formatCostUsd(project.cost_usd)}</b><small className="block text-muted-foreground">Cost</small></span></div>
      </article>
    ))} />
  );
}

function ProjectRow({ project }: { project: CodeTimeProject }) {
  return (
    <React.Fragment>
      <tr className="border-b border-border-subtle hover:bg-accent/30">
        <td className="px-4 py-2 font-medium">
          <ProjectLink name={project.project_name} />
        </td>
        <td className="px-4 py-2 text-right tabular-nums">{project.session_count}</td>
        <td className="px-4 py-2 text-right tabular-nums">{durationOrDash(project.execution_seconds)}</td>
        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">{durationOrDash(project.wait_seconds)}</td>
        <td className="px-4 py-2 text-right tabular-nums">{project.turns}</td>
        <td className="px-4 py-2 text-right tabular-nums">{project.tool_calls}</td>
        <td className="px-4 py-2 text-right tabular-nums">{formatCompactNumber(project.tokens.processed_tokens)}</td>
        <td className="px-4 py-2 text-right tabular-nums">{formatCostUsd(project.cost_usd)}</td>
      </tr>
      {project.sessions.map((session) => (
        <tr key={session.root_session_id} className="border-b border-border-subtle/50 text-caption text-muted-foreground">
          <td className="px-4 py-1.5 pl-8">
            <SessionLink sessionId={session.root_session_id} className="text-xs font-normal" />
            <span className="ml-2 text-foreground">{session.title || "-"}</span>
          </td>
          <td className="px-4 py-1.5 text-right" />
          <td className="px-4 py-1.5 text-right tabular-nums">{durationOrDash(session.execution_seconds)}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{durationOrDash(session.wait_seconds)}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{session.turns}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{session.tool_calls}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{formatCompactNumber(session.tokens.processed_tokens)}</td>
          <td className="px-4 py-1.5 text-right tabular-nums">{formatCostUsd(session.cost_usd)}</td>
        </tr>
      ))}
    </React.Fragment>
  );
}
