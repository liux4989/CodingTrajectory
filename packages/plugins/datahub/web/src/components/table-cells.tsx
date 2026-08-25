import * as React from "react";
import { cn } from "@/lib/utils";

/** Uppercase table header label, optionally right-aligned. */
export function HeaderLabel({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "right" }) {
  return (
    <span className={cn("label-uppercase", align === "right" && "text-right")}>
      {children}
    </span>
  );
}

/** Right-aligned cell content for numeric columns. */
export function RightCell({ children }: { children: React.ReactNode }) {
  return <div className="text-right">{children}</div>;
}

/** Eyebrow-style filter label wrapping a control. */
export function FilterLabel({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="eyebrow-soft grid gap-1 text-muted-foreground">
      {label}
      {children}
    </label>
  );
}
