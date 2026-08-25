# Invocation Log

## Goal

Capture every `ct` CLI invocation as a single structured record written to a
global, append-only log. The log is the maintenance telemetry channel: it lets
the project owner understand how the CLI is actually used across projects,
folders, vendors, and sessions — without asking users to opt into per-command
reporting or to inspect terminal output after the fact.

This log is *not* a user-facing output. It is infrastructure for the owner of
the tool.

## Why a dedicated log

- CLI output formats (markdown reports, compact JSON payloads) exist to help
  the LLM and the user read the current session. Mixing maintenance telemetry
  into those surfaces pollutes both.
- The existing index cache (`~/.coding-trajectory/index.json`) is a lookup
  cache, not an event log. It records `path → session_graph` and
  `session → session_graph` for store resolution; it does not record who ran
  what, when, or from where.
- Warnings were previously rendered into the markdown output of
  `session stats` and `session usage`. That surface was optimized for the
  reader, not for aggregation. A per-invocation log is the right home for
  degraded-output signals.

## Storage

| Property | Value |
|---|---|
| Path | `~/.coding-trajectory/invocations.jsonl` |
| Format | Newline-delimited JSON, one record per invocation |
| Scope | Global — collects invocations from every working directory |
| Write mode | Append-only writes coordinated by a sibling lock file |
| Rotation | On every logged invocation: drop records older than 30 days, then drop oldest retained records until the file plus the incoming record fits within 10 MiB |

A single global path is important: the value of the log is that the same owner
runs `ct` from many projects and folders, and the log collapses that activity
into one queryable surface.

The writer keeps append safety as the default posture: it acquires a sibling
lock file, rewrites the log only when pruning is required, then appends the
new record with `O_APPEND`. Rotation is therefore coordinated across `ct`
processes without making `doctor` a mutating command.

Corrupt records are never removed by the writer. If a malformed line is
present, the writer preserves the file byte-for-byte and appends only when the
result remains within 10 MiB. Otherwise it skips the new telemetry record so
`ct doctor` can continue to report the original corruption. A single incoming
record larger than 10 MiB is also skipped.

## Record schema

One record is written per `ct` invocation, after the command completes (or
after it fails). Fields:

| Field | Type | Notes |
|---|---|---|
| `ts` | ISO-8601 string | Invocation end time, UTC |
| `ct_version` | string | Package version of the CLI |
| `cwd` | string | Working directory the command was run from |
| `cmd` | string | Public command path, e.g. `session.stats`, `project.list` |
| `method` | string | Service method dispatched to, e.g. `session.stats` |
| `session_id` | string or null | Root session ID resolved for the invocation |
| `vendor` | string or null | Agent vendor when a single vendor applies |
| `exit_code` | integer | Process exit status returned to the shell |
| `ok` | boolean | `true` if the invocation completed without error |
| `error` | string or null | Short error class or message when `ok` is `false` |
| `ms` | integer | Wall-clock duration of the invocation, in milliseconds |
| `warnings` | array of objects | Warnings emitted via `debug.warn` during execution (see below) |

Each entry in `warnings` is a structured record:

| Field | Type | Notes |
|---|---|---|
| `message` | string | Human-readable description |
| `code` | string or null | Stable grouping identifier |
| `severity` | string | `info`, `warning`, or `error` |
| `context` | object | Arbitrary structured data from the emission site |

`exit_code` and `ok` are intentionally separate. Commands such as `ct doctor`
use non-zero exit codes to signal diagnostic status without meaning the
invocation itself failed. Those runs are recorded with `ok=true` and the
diagnostic `exit_code`, while unreadable state or handler failures set
`ok=false`.

The record is deliberately narrow. It does not contain command arguments,
command output, session content, or user identity. Those belong in richer
debugging surfaces, not the maintenance log.

## Warning emission: `debug.warn`

Warnings are not scraped from specific payload fields by name. Instead, any
layer of the system — adapter, analysis, metrics, service, or CLI — emits
warnings through a single primitive:

```
debug.warn(message, *, code=None, severity="warning", **context)
```

`debug` is a per-invocation context object, created at the start of each CLI
dispatch and scoped to that invocation. Internally it is a bounded, ordered
list of warning records. The emission site does not know where warnings are
routed — it only appends to the current invocation's list.

Because emission is uniform, this document does not enumerate which code paths
produce warnings. Adding a new warning is a single call at the point the
degraded condition is observed; no schema change, no dispatch-site wiring, no
CLI change.

### Severity levels

| Severity | Meaning |
|---|---|
| `info` | A non-default path was taken; no impact on output correctness. |
| `warning` | Output is degraded or incomplete but still usable. |
| `error` | A recoverable failure happened; some output was skipped. |

Hard failures that abort the invocation are reported via the `ok` / `error`
fields on the invocation record itself, not through `debug.warn`.

### Context fields

`code` is an optional stable identifier for grouping warnings across runs
(e.g. `cost.pricing_missing`, `context.no_observation`, `adapter.unknown_event`).
Additional keyword arguments are recorded as structured context on the warning
record and are available for aggregation by downstream consumers.

## Collection point

The CLI has a single dispatch path in `cli.py` that resolves the store and
invokes the service. A `debug` context is created at the top of that path and
propagated (via a contextvar or explicit parameter) to every layer that may
emit warnings. The invocation log is written in a `try/finally` block that
captures duration, outcome, and the accumulated warnings regardless of success
or failure.

Individual commands and service methods call `debug.warn` when they observe a
degraded condition. They do not know the log exists and do not write to it
directly.

## Telemetry configuration precedence

The log is local to the owner's machine, but an explicit opt-out is still
expected:

- `CT_TELEMETRY` is authoritative when it is set to a non-empty value.
- Otherwise `~/.coding-trajectory/config.toml` may set `telemetry.enabled`.
- Otherwise telemetry is enabled by default.

Examples:

- `CT_TELEMETRY=0` disables writes even if `config.toml` enables telemetry.
- `CT_TELEMETRY=1` enables writes even if `config.toml` disables telemetry.
- `telemetry.enabled = false` disables writes only when `CT_TELEMETRY` is unset
  or empty.

If `config.toml` is malformed or the `telemetry.enabled` value is invalid, the
writer falls back to the default-enabled behavior and records the config error
state for `ct doctor` to report as an environment failure.

When disabled, the CLI behaves exactly as it does today — no writes, no file
creation, no side effects.

## Consumption

The canonical CLI-side consumer of the invocation log is `ct doctor`
(see [`doctor.md`](doctor.md)), which aggregates the log into environment,
invocation, failure, warning, and latency sections.

Outside the CLI, the log remains open to ad-hoc tooling:

- Standard JSONL pipelines (`jq`, `pandas`, SQL-over-JSONL).
- The datahub plugin, as a future visualization source (usage over time,
  warnings by vendor, latency regressions across versions).
- Periodic maintenance reports summarizing which commands run, which vendors
  produce the most degraded output, and where adapter work should be focused.

No other CLI command reads the log directly. Adding new log-reading commands
should be weighed against keeping `doctor` the single diagnostic surface.

## What this replaces

- Warning rendering in `session stats` and `session usage` markdown output is
  removed. The warnings still exist inside the service payload; they are
  routed to the invocation log instead of to the user.
- The JSON output of `session stats` and `session usage` continues to carry
  the `warnings` field for programmatic callers that want the per-call signal.
  The invocation log is the long-term aggregation layer on top of that.

## Maintenance questions the log answers

- Which commands run most often, and which are unused?
- Which agent vendors produce the most degraded output?
- Are warnings clustering around specific adapters or session shapes?
- Has a CLI release introduced a latency regression?
- How is `ct` activity distributed across projects and working directories?

## Non-goals

- No user identity tracking. The log is for a single-owner tool.
- No remote upload. The log stays on disk.
- No per-command query surface. Consumption is out-of-band.
- No replacement for the index cache. The cache and the log serve different
  purposes and are stored in separate files.
