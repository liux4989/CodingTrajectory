import * as React from "react";
import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";

type ProjectLinkProps = {
  name: string;
  className?: string;
  children?: React.ReactNode;
};

export function ProjectLink({ name, className, children }: ProjectLinkProps) {
  return (
    <Link
      to="/sessions"
      search={{ projectName: name }}
      className={cn("link", className)}
    >
      {children ?? name}
    </Link>
  );
}
