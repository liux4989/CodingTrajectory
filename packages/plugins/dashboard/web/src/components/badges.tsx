import * as React from "react";
import { Badge } from "@/components/ui/badge";

export function VendorBadges({ vendors }: { vendors: string[] }) {
  if (!vendors.length) return <span className="text-muted-foreground">-</span>;
  return (
    <div className="flex flex-wrap gap-2">
      {vendors.map((vendor) => (
        <Badge key={vendor} variant="secondary">{vendor}</Badge>
      ))}
    </div>
  );
}

export function ReasonBadges({ reasons }: { reasons: string[] }) {
  return (
    <div className="flex flex-wrap gap-2">
      {reasons.map((reason) => (
        <Badge key={reason} variant="secondary">{reason}</Badge>
      ))}
    </div>
  );
}

export function ReasonSummary({ title, reasons }: { title: string; reasons: Record<string, number> }) {
  const entries = Object.entries(reasons);
  return (
    <div className="mt-4 grid gap-2">
      <h3 className="m-0 font-display font-semibold">{title}</h3>
      {entries.length ? (
        <div className="flex flex-wrap gap-2">
          {entries.map(([reason, count]) => (
            <Badge key={reason} variant="secondary">
              {reason} <strong>{count}</strong>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-muted-foreground">No skip reasons.</p>
      )}
    </div>
  );
}
