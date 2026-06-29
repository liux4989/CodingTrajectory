import * as React from "react";
import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";

export function shortSessionId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 12 ? value.slice(0, 12) : value;
}

type SessionLinkProps = {
  sessionId: string | null | undefined;
  className?: string;
  children?: React.ReactNode;
};

export function SessionLink({ sessionId, className, children }: SessionLinkProps) {
  if (!sessionId) {
    return <span className={className}>-</span>;
  }
  return (
    <Link
      to="/sessions/$sessionId/context-window"
      params={{ sessionId }}
      className={cn(
        "font-display font-extrabold text-primary decoration-[0.08em] underline-offset-[0.2em]",
        className,
      )}
    >
      {children ?? shortSessionId(sessionId)}
    </Link>
  );
}
