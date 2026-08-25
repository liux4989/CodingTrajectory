import * as React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { GraphEdge, GraphSessionNode } from "@/api";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { formatLabel, formatTokens } from "@/lib/format";
import { relativeTime } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

type TreeItem = {
  node: GraphSessionNode;
  children: TreeItem[];
};

function buildTree(nodes: GraphSessionNode[]): TreeItem[] {
  const byId = new Map(nodes.map((node) => [node.session_id, node]));
  const items = new Map<string, TreeItem>(
    nodes.map((node) => [node.session_id, { node, children: [] }]),
  );
  const roots: TreeItem[] = [];
  for (const item of items.values()) {
    const parent = item.node.parent_session_id;
    const parentItem = parent ? items.get(parent) : undefined;
    if (parent && byId.has(parent) && parentItem) {
      parentItem.children.push(item);
    } else {
      roots.push(item);
    }
  }
  const byStartedAt = (a: TreeItem, b: TreeItem) =>
    (a.node.started_at ?? "").localeCompare(b.node.started_at ?? "");
  roots.sort(byStartedAt);
  for (const item of items.values()) item.children.sort(byStartedAt);
  return roots;
}

function nodeLabel(node: GraphSessionNode) {
  return node.title || node.agent_name || shortSessionId(node.session_id);
}

type GraphTreeProps = {
  nodes: GraphSessionNode[];
  edges: GraphEdge[];
  /** Processed tokens per session, when usage facts are available. */
  tokensBySession?: ReadonlyMap<string, number>;
  activeSessionId?: string;
};

/**
 * Interactive session hierarchy. Tree connectors carry the structural edges:
 * the chip on each child row is the observed relationship to its parent, with
 * provenance details on hover. Edges that do not map onto the hierarchy are
 * listed separately below the tree.
 */
export function GraphTree({ nodes, edges, tokensBySession, activeSessionId }: GraphTreeProps) {
  const [collapsed, setCollapsed] = React.useState<ReadonlySet<string>>(new Set());
  const roots = React.useMemo(() => buildTree(nodes), [nodes]);

  const hierarchyEdges = React.useMemo(() => {
    const parentBySession = new Map(
      nodes.map((node) => [node.session_id, node.parent_session_id ?? null]),
    );
    const byTarget = new Map<string, GraphEdge[]>();
    const detached: GraphEdge[] = [];
    for (const edge of edges) {
      const target = edge.target_session_id ?? "";
      if (target && (parentBySession.get(target) ?? null) === (edge.source_session_id ?? null)) {
        byTarget.set(target, [...(byTarget.get(target) ?? []), edge]);
      } else {
        detached.push(edge);
      }
    }
    return { byTarget, detached };
  }, [nodes, edges]);

  const toggle = (id: string) => {
    const next = new Set(collapsed);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    setCollapsed(next);
  };

  const renderRow = (item: TreeItem) => {
    const { node } = item;
    const hasChildren = item.children.length > 0;
    const isCollapsed = collapsed.has(node.session_id);
    const nodeEdges = hierarchyEdges.byTarget.get(node.session_id) ?? [];
    const tokens = tokensBySession?.get(node.session_id);
    const isActive = node.session_id === activeSessionId;

    return (
      <div
        role="treeitem"
        aria-expanded={hasChildren ? !isCollapsed : undefined}
        aria-selected={isActive}
        className={cn(
          "flex min-w-0 flex-wrap items-center gap-2 rounded-lg px-2 py-1.5",
          isActive && "bg-surface-emphasis",
        )}
      >
        {hasChildren ? (
          <button
            type="button"
            onClick={() => toggle(node.session_id)}
            aria-label={isCollapsed ? "Expand session children" : "Collapse session children"}
            className="inline-flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-surface-emphasis hover:text-foreground"
          >
            {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </button>
        ) : (
          <span className="inline-flex size-5 shrink-0 items-center justify-center text-muted-foreground">
            <span className="size-1 rounded-full bg-current" />
          </span>
        )}
        <SessionLink sessionId={node.session_id} className="truncate">
          {nodeLabel(node)}
        </SessionLink>
        {node.vendor ? <Badge variant="secondary">{node.vendor}</Badge> : null}
        {node.model ? <Badge variant="secondary">{node.model}</Badge> : null}
        {node.reasoning_effort ? (
          <Badge variant="outline">{formatLabel(node.reasoning_effort)}</Badge>
        ) : null}
        {nodeEdges.length > 0 ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge variant="outline" className="cursor-default">
                {formatLabel(nodeEdges[0].type)}
              </Badge>
            </TooltipTrigger>
            <TooltipContent>
              {nodeEdges.map((edge, index) => (
                <span key={index} className="block">
                  {edge.provenance ?? "unknown provenance"}
                  {edge.confidence ? ` · ${edge.confidence} confidence` : ""}
                </span>
              ))}
            </TooltipContent>
          </Tooltip>
        ) : (
          <Badge variant="outline">Root</Badge>
        )}
        {tokens != null ? (
          <span className="mono text-caption tabular-nums text-muted-foreground">
            {formatTokens(tokens)}
          </span>
        ) : null}
        <span className="text-caption text-muted-foreground">
          {node.status ?? "unknown"} · {relativeTime(node.started_at)}
        </span>
      </div>
    );
  };

  const renderBranch = (item: TreeItem): React.ReactNode => {
    const isCollapsed = collapsed.has(item.node.session_id);
    return (
      <div key={item.node.session_id}>
        {renderRow(item)}
        {item.children.length > 0 && !isCollapsed ? (
          <div role="group" className="ml-[0.9rem] border-l border-border-soft pl-2">
            {item.children.map(renderBranch)}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <TooltipProvider>
      <div role="tree" aria-label="Session hierarchy" className="grid gap-1">
        {roots.map(renderBranch)}
      </div>
      {edges.length === 0 ? (
        <p className="m-0 mt-3 text-body-sm text-muted-foreground">
          No structural edges observed.
        </p>
      ) : null}
      {hierarchyEdges.detached.length > 0 ? (
        <div className="mt-3 grid gap-1 border-t border-border-soft pt-3">
          <p className="m-0 text-caption text-muted-foreground">
            Observed edges outside the hierarchy
          </p>
          {hierarchyEdges.detached.map((edge, index) => (
            <div key={index} className="flex flex-wrap items-center gap-2 text-body-sm">
              <Badge variant="outline">{formatLabel(edge.type)}</Badge>
              <SessionLink sessionId={edge.source_session_id} />
              <span className="text-muted-foreground">→</span>
              <SessionLink sessionId={edge.target_session_id} />
              <span className="text-caption text-muted-foreground">
                {edge.provenance ?? "-"}
                {edge.confidence ? ` · ${edge.confidence}` : ""}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </TooltipProvider>
  );
}
