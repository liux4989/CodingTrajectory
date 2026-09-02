# Supabase database workflow

This directory is the version-controlled Supabase CLI configuration for
CodingTrajectory. The local configuration uses PostgreSQL 17, matching the
currently visible Supabase project's database major version.

## Source-of-truth boundary

CodingTrajectory does not currently define a remotely deployed relational
schema. Its SQLite files are disposable, revisioned read models rebuilt from
canonical JSONL agent-session logs. Do not convert or upload those SQLite files
as a Supabase migration: that would turn a local cache into a production
authority and would change the product's data-ownership model.

Create a migration only when a new, explicitly owned Supabase data model has
been designed. Keep each migration under `supabase/migrations/` and review its
SQL before deployment.

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
