import * as React from "react";
import { Badge } from "./ui/badge";

export function VendorBadges({ vendors }: { vendors: string[] }) {
  if (!vendors.length) return <span className="muted">-</span>;
  return (
    <div className="badge-cloud">
      {vendors.map((vendor) => (
        <Badge key={vendor} variant="quiet">{vendor}</Badge>
      ))}
    </div>
  );
}

export function ReasonBadges({ reasons }: { reasons: string[] }) {
  return (
    <div className="badge-cloud">
      {reasons.map((reason) => (
        <Badge key={reason} variant="quiet">{reason}</Badge>
      ))}
    </div>
  );
}

export function ReasonSummary({ title, reasons }: { title: string; reasons: Record<string, number> }) {
  const entries = Object.entries(reasons);
  return (
    <div className="reason-summary">
      <h3>{title}</h3>
      {entries.length ? (
        <div className="badge-cloud">
          {entries.map(([reason, count]) => (
            <Badge key={reason} variant="quiet">
              {reason} <strong>{count}</strong>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="muted">No skip reasons.</p>
      )}
    </div>
  );
}
