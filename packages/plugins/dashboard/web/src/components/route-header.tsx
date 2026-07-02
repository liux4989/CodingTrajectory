import * as React from "react";
import { motion } from "motion/react";
import { fadeSoft } from "@/lib/motion";
import { cn } from "@/lib/utils";

const headerSurface =
  "border border-border-soft bg-[image:var(--gradient-header)] p-[clamp(1rem,3vw,2.2rem)] shadow-popover";

type RouteHeaderProps = {
  eyebrow: string;
  title: string;
  action?: React.ReactNode;
};

export function RouteHeader({ eyebrow, title, action }: RouteHeaderProps) {
  return (
    <motion.header
      variants={fadeSoft}
      initial="hidden"
      animate="visible"
      className={cn("flex items-start justify-between gap-4 rounded-3xl", headerSurface)}
    >
      <div>
        <motion.p variants={fadeSoft} className="eyebrow mb-1 text-primary">
          {eyebrow}
        </motion.p>
        <motion.h2
          variants={fadeSoft}
          className="m-0 max-w-[18ch] font-display text-[clamp(2rem,5vw,5.25rem)] leading-tight tracking-tight text-wrap-balance"
        >
          {title}
        </motion.h2>
      </div>
      {action}
    </motion.header>
  );
}
