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
      to="/projects/$projectName"
      params={{ projectName: name }}
      className={cn("text-primary decoration-[0.08em] underline-offset-[0.2em]", className)}
    >
      {children ?? name}
    </Link>
  );
}
