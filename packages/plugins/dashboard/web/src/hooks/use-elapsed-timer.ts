import * as React from "react";

/**
 * Counts elapsed milliseconds while `active` is true. Resets to 0 whenever a
 * new `active` cycle begins (false -> true). Used by loading UI to show how
 * long an operation has been running.
 */
export function useElapsedTimer(active: boolean): number {
  const [elapsed, setElapsed] = React.useState(0);
  const startRef = React.useRef<number | null>(null);

  React.useEffect(() => {
    if (!active) {
      startRef.current = null;
      setElapsed(0);
      return;
    }
    startRef.current = performance.now();
    setElapsed(0);
    const id = window.setInterval(() => {
      if (startRef.current != null) {
        setElapsed(performance.now() - startRef.current);
      }
    }, 200);
    return () => window.clearInterval(id);
  }, [active]);

  return elapsed;
}

export function formatElapsed(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes > 0) {
    return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  }
  return `${seconds}s`;
}
