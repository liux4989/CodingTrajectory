import * as React from "react";
import { Loader2, X } from "lucide-react";
import { motion } from "motion/react";
import { fadeUp } from "@/lib/motion";
import { formatElapsed } from "@/hooks/use-elapsed-timer";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type LoadingStateProps = {
  title: string;
  detail?: string;
  /** Elapsed milliseconds, shown as a running mm:ss timer. */
  elapsedMs?: number;
  /** Optional progress label from the server (e.g. "running skill turn…"). */
  progress?: string | null;
  /** When provided, renders a cancel button. */
  onCancel?: () => void;
  className?: string;
};

/**
 * Reusable loading surface for any pending server operation. Shows a spinner,
 * a running elapsed timer, optional server progress, and an optional cancel
 * button. Used for both query-pending and job-pending states.
 */
export function LoadingState({
  title,
  detail,
  elapsedMs,
  progress,
  onCancel,
  className,
}: LoadingStateProps) {
  return (
    <motion.div variants={fadeUp} initial="hidden" animate="visible">
      <Card className={cn("min-w-0", className)}>
        <CardContent className="flex items-center gap-3">
          <Loader2 size={18} className="shrink-0 animate-spin text-primary" />
          <div className="min-w-0 flex-1">
            <p className="m-0 title-state">{title}</p>
            {detail ? <p className="m-0 text-body-sm text-muted-foreground">{detail}</p> : null}
            {progress ? (
              <p className="m-0 mt-1 text-caption text-muted-foreground">{progress}</p>
            ) : null}
          </div>
          {elapsedMs != null && elapsedMs > 0 ? (
            <span className="shrink-0 mono text-caption text-muted-foreground">
              {formatElapsed(elapsedMs)}
            </span>
          ) : null}
          {onCancel ? (
            <Button size="icon-sm" variant="ghost" onClick={onCancel} aria-label="Cancel">
              <X size={15} />
            </Button>
          ) : null}
        </CardContent>
      </Card>
    </motion.div>
  );
}
