import * as React from "react";
import { motion } from "motion/react";
import { fadeSoft } from "@/lib/motion";

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
      className="flex items-start justify-between gap-4 rounded-3xl border border-foreground/13 bg-[linear-gradient(135deg,rgb(255_249_234/94%),rgb(215_200_164/34%)),var(--paper-strong)] p-[clamp(1rem,3vw,2.2rem)] shadow-[var(--shadow),0_24px_70px_rgb(49_42_25/18%)] dark:border-border-subtle dark:bg-[linear-gradient(135deg,rgb(34_32_25/94%),rgb(58_54_44/34%)),var(--paper-strong)] dark:shadow-[0_24px_70px_rgb(0_0_0/40%)]"
    >
      <div>
        <motion.p
          variants={fadeSoft}
          className="mb-1 font-display text-eyebrow font-extrabold uppercase tracking-wider text-primary"
        >
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
