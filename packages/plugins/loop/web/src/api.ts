import { useEffect, useState } from "react";
import type { CanonicalReference } from "./generated/investigation";

export type CoreResult<T> = { result: T; meta?: Record<string, unknown> };

export async function request<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false)
    throw new Error(
      typeof data.error === "string"
        ? data.error
        : JSON.stringify(data.error ?? "Local query failed"),
    );
  return data;
}

export function useCore<T>(
  method: string,
  params: Record<string, unknown>,
  enabled = true,
) {
  const key = JSON.stringify(params);
  const requestKey = `${method}:${key}:${enabled}`;
  const [state, setState] = useState<{
    requestKey: string;
    data?: CoreResult<T>;
    error?: string;
    loading: boolean;
  }>({ loading: enabled, requestKey });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: enabled, requestKey });
    if (enabled)
      request<CoreResult<T>>(
        "/api/core",
        { method, params: JSON.parse(key) },
        controller.signal,
      )
        .then((data) => {
          if (!controller.signal.aborted)
            setState({ data, loading: false, requestKey });
        })
        .catch((error) => {
          if (!controller.signal.aborted)
            setState({
              error: String(error.message),
              loading: false,
              requestKey,
            });
        });
    return () => controller.abort();
  }, [method, key, enabled, requestKey]);
  return state.requestKey === requestKey
    ? state
    : { loading: enabled, data: undefined, error: undefined, requestKey };
}

export function referenceLink(
  reference: CanonicalReference,
  investigationId?: string | null,
) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(reference))
    if (value) params.set(key, value);
  if (investigationId) params.set("investigation_id", investigationId);
  return `#${params}`;
}

export function readReference(): CanonicalReference | null {
  const query = new URLSearchParams(location.hash.slice(1));
  const session_id = query.get("session_id");
  if (!session_id) return null;
  return {
    session_id,
    turn_id: query.get("turn_id"),
    item_id: query.get("item_id"),
    event_id: query.get("event_id"),
  };
}

// Core intentionally leaves the overview display dictionaries open. These small
// readers inspect that display, without declaring a second canonical schema.
export function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function rows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(object) : [];
}
export function string(value: unknown) {
  return typeof value === "string" ? value : "";
}
