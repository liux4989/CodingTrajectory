import * as React from "react";
import { Bot, Box, MessageSquare, User, Wrench } from "lucide-react";
import type { SessionTimelineEntry, TimelineArtifactKind, TimelineKind } from "@/api";
import { shortSessionId } from "@/components/session-link";
import { relativeTime } from "@/lib/relative-time";

export type OutcomeFilter = "all" | "failed" | "succeeded";

export function agentLabel(entry: SessionTimelineEntry) {
  return entry.agent_name || entry.vendor || shortSessionId(entry.session_id);
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
