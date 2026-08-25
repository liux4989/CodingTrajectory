import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { Bot, GitBranch } from "lucide-react";
import { fetchSessionTree, type ConversationBranch } from "@/api";
import { LoadingState } from "@/components/loading-state";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { SessionViewTabs } from "@/components/session-view-tabs";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { relativeTime } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

type BranchNode = ConversationBranch & { children: BranchNode[] };

function branchTree(branches: ConversationBranch[]): BranchNode[] {
  const nodes = new Map<string, BranchNode>(
    branches.map((branch) => [
      branch.session_id,
      { ...branch, children: [] } as BranchNode,
    ]),
  );
  const roots: BranchNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parent_session_id
      ? nodes.get(node.parent_session_id)
      : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const byTime = (left: BranchNode, right: BranchNode) =>
    (left.started_at ?? "").localeCompare(right.started_at ?? "");
  roots.sort(byTime);
  for (const node of nodes.values()) node.children.sort(byTime);
  return roots;
}

function branchLabel(branch: ConversationBranch) {
  return branch.title || branch.agent_name || shortSessionId(branch.session_id);
}

function ConversationTree({
  branches,
  selectedBranchId,
}: {
  branches: ConversationBranch[];
  selectedBranchId?: string;
}) {
  const roots = React.useMemo(() => branchTree(branches), [branches]);

  const renderBranch = (branch: BranchNode): React.ReactNode => {
    const selected = branch.session_id === selectedBranchId;
    const agentCount = branch.spawned_agent_count ?? 0;
    return (
      <div key={branch.session_id}>
        <div
          role="treeitem"
          aria-current={selected ? "true" : undefined}
          aria-expanded={branch.children.length ? true : undefined}
          className={cn(
            "flex min-w-0 flex-wrap items-center gap-2 rounded-lg px-3 py-2",
            selected && "bg-surface-emphasis",
          )}
        >
          <GitBranch aria-hidden="true" className="text-muted-foreground" />
          <SessionLink sessionId={branch.session_id} className="min-w-0 truncate">
            {branchLabel(branch)}
          </SessionLink>
          {selected ? <Badge>Selected branch</Badge> : null}
          {branch.vendor ? <Badge variant="secondary">{branch.vendor}</Badge> : null}
          <Badge variant="outline">
            {branch.turn_count ?? 0} turn{branch.turn_count === 1 ? "" : "s"}
          </Badge>
          <Badge variant="outline">
            {agentCount} agent{agentCount === 1 ? "" : "s"}
          </Badge>
          <span className="text-caption text-muted-foreground">
            {branch.status ?? "unknown"} · {relativeTime(branch.started_at)}
          </span>
          <div className="ml-auto flex flex-wrap gap-2">
            <Button asChild variant="outline" size="sm">
              <Link
                to="/sessions/$sessionId"
                params={{ sessionId: branch.session_id }}
              >
                Open session
              </Link>
            </Button>
            <Button asChild size="sm">
              <Link
                to="/sessions/$sessionId/graph"
                params={{ sessionId: branch.session_id }}
              >
                <Bot data-icon="inline-start" />
                Agent graph
              </Link>
            </Button>
          </div>
        </div>
        {branch.children.length ? (
          <div role="group" className="ml-5 border-l border-border-soft pl-3">
            {branch.children.map(renderBranch)}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div role="tree" aria-label="Conversation branches" className="grid gap-1">
      {roots.map(renderBranch)}
    </div>
  );
}

export function SessionTreeRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/tree" });
  const query = useQuery({
    queryKey: ["session-tree", sessionId],
    queryFn: () => fetchSessionTree(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  if (query.isPending) {
    return (
      <div className="route-container w-full min-w-0 pb-8">
        <LoadingState
          title="Loading conversation tree"
          detail="Reading ordinary fork relationships for this session family."
        />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container w-full min-w-0 pb-8">
        <StateBlock
          title="Conversation tree failed"
          detail={query.error.message}
          onRetry={() => query.refetch()}
        />
      </div>
    );
  }

  const payload = query.data;
  return (
    <div className="route-container w-full min-w-0 pb-8">
      <div className="grid gap-4">
        <div className="min-w-0">
          <h1 className="m-0 font-display text-h1 leading-tight">
            Conversation tree
          </h1>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            Ordinary human forks stay separate; each branch owns its agent graph.
          </p>
        </div>
        <SessionViewTabs sessionId={sessionId} active="tree" />
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Conversation branches</CardTitle>
            <CardDescription>
              Select a branch to inspect its conversation, or open only the agents
              spawned from that branch.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {payload.branches.length ? (
              <ConversationTree
                branches={payload.branches}
                selectedBranchId={payload.selected_branch_id}
              />
            ) : (
              <p className="m-0 text-body-sm text-muted-foreground">
                No conversation branches observed.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
