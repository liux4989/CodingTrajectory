import * as React from "react";
import {
  animate,
  useMotionValue,
  useTransform,
  motion,
  useReducedMotion,
} from "motion/react";

type AnimatedNumberProps = {
  value: number;
  /** A formatter applied to the live interpolated value. */
  format?: (value: number) => string;
  className?: string;
};

/**
 * Smoothly tweens a numeric value into view whenever it changes, rendering the
 * formatted result. Falls back to a static render when the user prefers
 * reduced motion.
 */
export function AnimatedNumber({ value, format, className }: AnimatedNumberProps) {
  const reduce = useReducedMotion();
  const mv = useMotionValue(value);
  const display = useTransform(mv, (latest) =>
    format ? format(latest) : Math.round(latest).toLocaleString(),
  );

  React.useEffect(() => {
    if (reduce) {
      mv.set(value);
      return;
    }
    const controls = animate(mv, value, {
      duration: 0.9,
      ease: [0.22, 1, 0.36, 1],
    });
    return () => controls.stop();
  }, [value, mv, reduce]);

  return <motion.span className={className}>{display}</motion.span>;
}
