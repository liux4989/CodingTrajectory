import * as React from "react";
type PageHeaderProps = {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: React.ReactNode;
};

export function PageHeader({ eyebrow, title, description, actions }: PageHeaderProps) {
  return (
    <header className="flex min-h-16 flex-wrap items-center justify-between gap-3 border-b border-border-subtle pb-3">
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow-soft mb-1 text-muted-foreground">{eyebrow}</p> : null}
        <h1 className="m-0 font-display text-h1 font-semibold leading-tight tracking-tight">{title}</h1>
        {description ? <p className="m-0 mt-1 text-body-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions}
    </header>
  );
}
