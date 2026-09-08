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
  if (vendor === "amp") return "border-vendor-amp/30 bg-vendor-amp/10 text-vendor-amp";
  if (vendor === "claude_code")
    return "border-vendor-claude-code/30 bg-vendor-claude-code/10 text-vendor-claude-code";
  if (vendor === "codex_cli")
    return "border-vendor-codex-cli/30 bg-vendor-codex-cli/10 text-vendor-codex-cli";
  if (vendor === "pi") return "border-vendor-pi/30 bg-vendor-pi/10 text-vendor-pi";
  return "border-border bg-secondary text-secondary-foreground";
}

/** Tokenized accent for an evidence kind; failures override with destructive. */
export function kindDotClass(kind: TimelineKind) {
  if (kind === "user") return "border-kind-user text-kind-user";
  if (kind === "assistant") return "border-kind-assistant text-kind-assistant";
  if (kind === "subagent") return "border-kind-subagent text-kind-subagent";
  if (kind === "compaction") return "border-kind-compaction text-kind-compaction";
  return "border-kind-tool text-kind-tool";
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
