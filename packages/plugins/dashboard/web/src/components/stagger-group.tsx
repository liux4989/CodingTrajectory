import * as React from "react";
import { motion } from "motion/react";
import { staggerContainer } from "@/lib/motion";

type StaggerGroupProps = {
  children: React.ReactNode;
  className?: string;
  /** Delay before the first child animates (seconds). */
  delay?: number;
  /** Gap between each child (seconds). */
  stagger?: number;
};

/**
 * Wraps children so any `motion` element using `staggerItem` (or other)
 * variants cascades in on mount. Respects reduced motion automatically via
 * the variant definitions.
 */
export function StaggerGroup({
  children,
  className,
  delay = 0.04,
  stagger = 0.06,
}: StaggerGroupProps) {
  return (
    <motion.div
      className={className}
      variants={{
        hidden: {},
        visible: { transition: { staggerChildren: stagger, delayChildren: delay } },
      }}
      initial="hidden"
      animate="visible"
    >
      {children}
    </motion.div>
  );
}
