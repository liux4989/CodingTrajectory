import * as React from "react";
import { Bot, Box, MessageSquare, User, Wrench } from "lucide-react";
import type { SessionTimelineEntry, TimelineArtifactKind, TimelineKind } from "@/api";
import { shortSessionId } from "@/components/session-link";
import { relativeTime } from "@/lib/relative-time";

export type OutcomeFilter = "all" | "failed" | "succeeded";

export function agentLabel(entry: SessionTimelineEntry) {
  return entry.agent_name || entry.vendor || shortSessionId(entry.session_id);
}

export function vendorLabel(vendor: string) {
  if (vendor === "amp") return "Amp";
  if (vendor === "claude_code") return "Claude Code";
  if (vendor === "codex_cli") return "Codex CLI";
  if (vendor === "pi") return "Pi";
  return vendor;
}

export function vendorBadgeClass(vendor: string) {
  if (vendor === "amp") return "border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300";
  if (vendor === "claude_code") return "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  if (vendor === "codex_cli") return "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300";
  if (vendor === "pi") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  return "border-border bg-secondary text-secondary-foreground";
}

export function kindLabel(kind: TimelineKind) {
  if (kind === "user") return "User";
  if (kind === "assistant") return "Assistant";
  if (kind === "tool") return "Tool";
  if (kind === "subagent") return "Child agent";
  if (kind === "compaction") return "Compaction";
  return kind;
}

export function artifactLabel(kind: TimelineArtifactKind) {
  if (kind === "file") return "File";
  if (kind === "command") return "Command";
  if (kind === "check") return "Check";
  if (kind === "commit") return "Commit";
  if (kind === "link") return "Link";
  return kind;
}

export function kindIcon(kind: TimelineKind, className = "size-2.5") {
  const Icon =
    kind === "user"
      ? User
      : kind === "assistant"
        ? MessageSquare
        : kind === "subagent"
          ? Bot
          : kind === "compaction"
            ? Box
            : Wrench;
  return <Icon aria-hidden="true" className={className} />;
}

export function isTerminalSuccess(status: string | null) {
  return Boolean(
    status && ["success", "succeeded", "done", "completed"].some((value) => status.toLowerCase().includes(value)),
  );
}

export function formatWhen(value: string | null) {
  if (!value) return "Recorded order";
  return relativeTime(value);
}
