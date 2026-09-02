# Supabase database workflow

This directory is the version-controlled Supabase CLI configuration for
CodingTrajectory. The local configuration uses PostgreSQL 17, matching the
currently visible Supabase project's database major version.

## Source-of-truth boundary

CodingTrajectory now defines the remote control-plane foundation in
`migrations/20260902000000_ct_control_plane_foundation.sql`. It owns workspace
identity, accepted observations, immutable graph revisions, portable projects,
living leases, estimation records, and transactional worker delivery.

Local SQLite files remain delivery state or disposable revision-bound read
models. Do not convert or upload them as Supabase migrations. Vendor logs remain
host-local upstream evidence and are published only through the future
authenticated collector protocol described in
[`docs/local-collector-handoff.md`](../docs/local-collector-handoff.md).

The target architecture and authority boundaries are documented in
[`docs/remote-ct-control-plane-design.md`](../docs/remote-ct-control-plane-design.md).
Keep each migration under `supabase/migrations/` and review its SQL before
deployment.

## Local development

Start a Docker-compatible runtime, then run:

```sh
supabase start
supabase status
```

Stop the stack without deleting local data with:

```sh
supabase stop
```

The generated `supabase/.temp/` directory is intentionally ignored. It records
machine-local CLI state, including the linked project reference.

## First remote deployment

Use the Supabase project ref from the intended project's Dashboard URL and a
database password obtained through the Dashboard. Do not commit either secret.
Copy the root `.env.example` to `.env` and set `SUPABASE_DB_PASSWORD` locally
before running the commands below.

```sh
supabase link --project-ref <project-ref> --password <database-password>
```

If that remote is an existing database, first reconcile its schema into a
reviewable migration and verify the resulting local history:

```sh
supabase db pull
supabase db reset
```

For a brand-new empty remote, skip the pull. In either case, inspect the
proposed deployment before changing the remote database:

```sh
supabase db push --dry-run --linked
supabase db push --linked
```

`db push` deploys only committed migration files; it does not publish the
project's current SQLite read models.
