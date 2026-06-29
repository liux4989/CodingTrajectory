import * as React from "react";
import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";

type ProjectLinkProps = {
  name: string;
  sinceDays?: number;
  className?: string;
  children?: React.ReactNode;
};

export function ProjectLink({ name, sinceDays, className, children }: ProjectLinkProps) {
  return (
    <Link
      to="/projects/$projectName"
      params={{ projectName: name }}
      search={{ sinceDays }}
      className={cn("text-primary decoration-[0.08em] underline-offset-[0.2em]", className)}
    >
      {children ?? name}
    </Link>
  );
}
