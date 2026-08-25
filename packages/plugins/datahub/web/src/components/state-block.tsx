import * as React from "react";
import { AlertCircle, RotateCcw } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

type StateBlockProps = {
  title: string;
  detail?: string;
  /** When provided, renders a retry button that calls this handler. */
  onRetry?: () => void;
  /** Label for the retry button. Defaults to "Retry". */
  retryLabel?: string;
};

/**
 * Reusable state surface for error or informational states. Supports an
 * optional retry action for recoverable errors.
 */
export function StateBlock({ title, detail, onRetry, retryLabel = "Retry" }: StateBlockProps) {
  return (
    <div>
      <Card className={onRetry ? "border-destructive/30" : undefined}>
        <CardContent className="flex items-start gap-3">
          {onRetry ? <AlertCircle size={18} className="mt-0.5 shrink-0 text-destructive" /> : null}
          <div className="min-w-0 flex-1 grid gap-2">
            <p className="m-0 title-state">{title}</p>
            {detail ? <p className="m-0 break-words text-body-sm text-muted-foreground">{detail}</p> : null}
          </div>
          {onRetry ? (
            <Button size="sm" variant="outline" onClick={onRetry} className="shrink-0 gap-1.5">
              <RotateCcw size={14} />
              {retryLabel}
            </Button>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
