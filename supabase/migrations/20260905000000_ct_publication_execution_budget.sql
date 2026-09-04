-- A bounded 4.74 MiB publication took 48.59s with all integrity checks enabled.
-- PostgREST hoists this setting for this RPC only; the authenticated role keeps
-- its normal 8s timeout. Size, digest, schema, topology and ownership gates stay
-- enforced, and a timed-out transaction remains an exact idempotent retry.
alter function public.ct_collector_publish_artifacts(jsonb, text, text)
  set statement_timeout = '60s';

notify pgrst, 'reload schema';
