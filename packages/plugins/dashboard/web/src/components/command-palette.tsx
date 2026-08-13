import * as React from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Search, CornerDownLeft } from "lucide-react";
import { fetchProjects, fetchSessions } from "@/api";
import { cn } from "@/lib/utils";

type CommandItem = {
  id: string;
  label: string;
  hint?: string;
  group: "Navigate" | "Sessions" | "Projects";
  onSelect: () => void;
};

/**
 * Command palette triggered by Cmd/Ctrl+K. Searches across routes, sessions,
 * and projects for quick navigation.
 */
export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const [query, setQuery] = React.useState("");
  const [activeIndex, setActiveIndex] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const listRef = React.useRef<HTMLDivElement>(null);

  const sessions = useQuery({
    queryKey: ["sessions", "command-palette", 7],
    queryFn: ({ signal }) => fetchSessions({ sinceDays: 7, limit: 50, signal }),
    enabled: open,
    staleTime: 60_000,
  });

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: fetchProjects,
    enabled: open,
    staleTime: 60_000,
  });

  const items = React.useMemo<CommandItem[]>(() => {
    const navItems: CommandItem[] = [
      { id: "nav-overview", label: "Overview", group: "Navigate", onSelect: () => navigate({ to: "/" }) },
      { id: "nav-sessions", label: "Sessions", group: "Navigate", onSelect: () => navigate({ to: "/sessions", search: { projectName: undefined } }) },
      { id: "nav-model-usage", label: "Usage", group: "Navigate", onSelect: () => navigate({ to: "/model-usage", search: { projectName: undefined, modelKey: undefined, view: undefined, grain: undefined, unit: undefined } }) },
    ];

    const sessionItems: CommandItem[] = (sessions.data?.items ?? [])
      .slice(0, 50)
      .map((s) => ({
        id: `session-${s.root_session_id}`,
        label: s.title || s.root_session_id.slice(0, 12),
        hint: [s.project, s.vendors.join(", ")].filter(Boolean).join(" · "),
        group: "Sessions" as const,
        onSelect: () => {
          navigate({ to: "/sessions/$sessionId/context-window", params: { sessionId: s.root_session_id } });
        },
      }));

    const projectItems: CommandItem[] = (projects.data?.items ?? [])
      .slice(0, 50)
      .map((p) => ({
        id: `project-${p.name}`,
        label: p.name,
        hint: p.path ?? undefined,
        group: "Projects" as const,
        onSelect: () => navigate({ to: "/sessions", search: { projectName: p.name } }),
      }));

    return [...navItems, ...sessionItems, ...projectItems];
  }, [navigate, sessions.data, projects.data]);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) =>
      `${item.label} ${item.hint ?? ""}`.toLowerCase().includes(q),
    );
  }, [items, query]);

  const grouped = React.useMemo(() => {
    const groups = new Map<string, CommandItem[]>();
    for (const item of filtered) {
      const arr = groups.get(item.group) ?? [];
      arr.push(item);
      groups.set(item.group, arr);
    }
    return Array.from(groups.entries());
  }, [filtered]);

  React.useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  React.useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = filtered[activeIndex];
      if (item) {
        item.onSelect();
        onOpenChange(false);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      onOpenChange(false);
    }
  }

  React.useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        onOpenChange(false);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onOpenChange]);

  if (!open) return null;

  let flatIndex = 0;

  return (
    <div
      className="fixed inset-0 z-[200] flex items-start justify-center p-4 pt-[15vh]"
      onClick={() => onOpenChange(false)}
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" />
      <div
        className="relative grid w-full max-w-xl gap-0 overflow-hidden rounded-2xl border border-border-soft bg-popover shadow-popover"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div className="flex items-center gap-3 border-b border-border-soft px-4 py-3">
          <Search size={18} className="shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search sessions, projects, or navigate..."
            className="min-w-0 flex-1 bg-transparent text-body outline-none placeholder:text-muted-foreground"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="hidden shrink-0 rounded border border-border-soft px-1.5 py-0.5 text-caption text-muted-foreground sm:inline">
            ESC
          </kbd>
        </div>
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto p-1">
          {filtered.length === 0 ? (
            <p className="px-3 py-6 text-center text-body-sm text-muted-foreground">
              No results for "{query}"
            </p>
          ) : (
            grouped.map(([group, groupItems]) => (
              <div key={group} className="grid gap-0.5 p-1">
                <p className="px-2 py-1 text-caption font-semibold uppercase tracking-wide text-muted-foreground">
                  {group}
                </p>
                {groupItems.map((item) => {
                  const idx = flatIndex++;
                  const isActive = idx === activeIndex;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={cn(
                        "flex items-center justify-between gap-3 rounded-lg px-2.5 py-2 text-left text-body-sm transition-colors",
                        isActive ? "bg-surface-emphasis text-foreground" : "text-foreground hover:bg-surface-subtle",
                      )}
                      onMouseEnter={() => setActiveIndex(idx)}
                      onClick={() => {
                        item.onSelect();
                        onOpenChange(false);
                      }}
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {item.label}
                        {item.hint ? (
                          <span className="ml-2 text-caption text-muted-foreground">{item.hint}</span>
                        ) : null}
                      </span>
                      {isActive ? <CornerDownLeft size={14} className="shrink-0 text-muted-foreground" /> : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
