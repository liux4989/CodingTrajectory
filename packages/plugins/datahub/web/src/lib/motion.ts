import type { Variants } from "motion/react";

/**
 * Shared motion tokens for the datahub. Designed to feel calm and fluent:
 * short durations, soft easing, small distances.
 *
 * Apply globally with `<MotionConfig reducedMotion="user">` (see main.tsx) so
 * every variant here is automatically disabled for users who request reduced
 * motion — no per-component handling required.
 */

/** Primary ease-out curve used across the datahub. */
export const EASE = [0.22, 1, 0.36, 1] as const;

/** A single element rising/fading into view. */
export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 12 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.42, ease: EASE } },
};

/** A gentler fade for headers and large display text. */
export const fadeSoft: Variants = {
  hidden: { opacity: 0, y: 6 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.5, ease: EASE } },
};

/** Page/route transition: slightly faster than fadeUp for navigation. */
export const pageTransition: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.34, ease: EASE } },
};

/** Stagger container: children animate in sequence as they mount. */
export const staggerContainer: Variants = {
  hidden: {},
  visible: {
    transition: { staggerChildren: 0.06, delayChildren: 0.04 },
  },
};

/** Item used inside a `staggerContainer`. */
export const staggerItem: Variants = {
  hidden: { opacity: 0, y: 14 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.4, ease: EASE } },
};

/** A scale-in for badges, pills and small accents. */
export const popIn: Variants = {
  hidden: { opacity: 0, scale: 0.92 },
  visible: { opacity: 1, scale: 1, transition: { duration: 0.32, ease: EASE } },
};
