import { ArrowUpRight } from "lucide-react";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyDescription,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import type { EvidenceReferences } from "./generated/session.summary";
import type { CanonicalReference } from "./generated/investigation";
import { referenceLink, readReference } from "./api";

export function Blank({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Empty>
      <EmptyHeader>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{children}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

export function ErrorNotice({ message }: { message?: string }) {
  return message ? (
    <Alert variant="destructive">
      <AlertTitle>Could not read local evidence</AlertTitle>
      <AlertDescription>
        {message} Return to Explore or reload to retry. No remote source was
        used.
      </AlertDescription>
    </Alert>
  ) : null;
}

export function Loading() {
  return (
    <div
      role="status"
      aria-label="Loading local evidence"
      className="flex flex-col gap-3 py-5"
    >
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-20 w-full" />
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

export function short(id: string) {
  return id.slice(0, 8);
}

export function date(value: unknown) {
  return typeof value === "string"
    ? new Date(value).toLocaleString()
    : "Time unavailable";
}

export function cite(ref: EvidenceReferences): CanonicalReference {
  return {
    session_id: ref.session_id,
    turn_id: ref.turn_id,
    item_id: ref.item_id,
    event_id: ref.item_id ? undefined : ref.event_ids?.[0],
  };
}

export function EvidenceLink({
  reference,
  children,
}: {
  reference: CanonicalReference;
  children: React.ReactNode;
}) {
  const investigationId =
    readReference()?.session_id === reference.session_id
      ? new URLSearchParams(location.hash.slice(1)).get("investigation_id")
      : null;
  return (
    <a
      className="evidence-link"
      href={referenceLink(reference, investigationId)}
    >
      {children}
      <ArrowUpRight aria-hidden="true" size={14} />
    </a>
  );
}
