# Doctor Command

## Goal

`ct doctor` is a single diagnostic surface that combines:

- **Invocation telemetry** — what `ct` has been doing on this machine.
- **Environment health** — whether the tool is configured correctly and can
  reach its data sources.

It is the first command the owner should run when something feels off, and the
periodic checkpoint for maintenance. It is also the only supported consumer
of the invocation log shipped inside the CLI itself; ad-hoc tooling is still
welcome, but `doctor` is the canonical view.

## Usage

```
ct doctor [--since DURATION] [--output markdown|json]
```

Defaults to markdown. `--since` accepts `7d`, `30d`, `90d`, `all`; default is
`7d`.

## Report sections

The markdown report is a fixed set of sections. Each section is self-contained
and does not cross-reference others; the goal is that any single section can be
read in isolation.

### 1. Environment

Verifies the runtime the CLI depends on.

| Check | Source |
|---|---|
| Python version | `sys.version_info` (must be >= 3.12) |
| CLI version | package metadata |
| Config files present | `~/.coding-trajectory/config.toml`, `~/.coding-trajectory/index.json` |
| Telemetry enabled | `CT_TELEMETRY` env and `config.toml` `telemetry.enabled` |
| Index cache size | number of entries, file size, last modified |
| Vendor log roots reachable | `~/.codex/sessions`, `~/.claude/projects`, `~/.pi/agent/sessions` |

Each check renders as `ok`, `warn`, or `fail`. The section has no aggregation
logic — it is a snapshot of the current environment.

### 2. Invocation summary

Aggregated statistics over the invocation log for the selected time window.

| Metric | Meaning |
|---|---|
| Total invocations | Count of records |
| Failed invocations | Count where `ok` is `false` |
| P50 / P95 latency | Milliseconds, across all invocations |
| Top commands | Top 5 by invocation count, with counts |
| Top working directories | Top 5 by invocation count, with counts |
| Vendors seen | Distinct vendors, with counts |

### 3. Failure summary

All records where `ok` is `false`, grouped by `error` and `cmd`. For each
group:

- The most recent occurrence (timestamp, cwd, session id).
- The total count in the window.
- A sample record, rendered compactly.

This section is the entry point for diagnosing recurring errors. It does not
attempt to explain causes — it surfaces patterns.

### 4. Warning summary

All warnings emitted via `debug.warn`, grouped by `code`. For each code:

- Count of occurrences.
- Severity distribution (`info` / `warning` / `error`).
- Top commands that produced the warning.
- One representative message.

Codes that appear only once are grouped under an "Other" bucket to keep the
section bounded.

### 5. Latency trends

Weekly or daily (depending on window size) median and P95 latency, split by
command. The goal is to surface regressions after a CLI release — a visible
step in the P95 line for `session.stats` is the kind of signal this section
exists to show.

Rendered as a small ASCII table; not a chart.

### 6. Stale state

Identifies entries in the index cache whose source files no longer exist on
disk. These are entries that would be pruned on the next load; surfacing them
here makes the pruning action visible and lets the owner trigger it
deliberately.

## JSON output

`--output json` emits a single object with one key per section. Each section's
payload is the structured data behind its markdown rendering — counts,
groupings, sample records. This is the stable programmatic contract for
consumers such as the dashboard plugin.

The JSON schema is versioned under a top-level `doctor_version` field.
Additive changes are non-breaking; section renames or semantic changes bump
the version.

## Relationship to the invocation log

`ct doctor` is the only CLI command that reads `invocations.jsonl`. The log
itself remains append-only infrastructure; `doctor` opens it read-only,
streams records through a bounded window, and discards them. It does not
modify, rotate, or delete log entries.

Rotation and size-based pruning of the log are performed by the invocation
log writer on every invocation, not by `doctor`. This keeps `doctor` a pure
consumer.

## Non-goals

- **No interactive mode.** `doctor` prints a report and exits. Interactive
  remediation belongs in a TUI, which is the dashboard plugin's job.
- **No auto-fix.** `doctor` may suggest actions (e.g. "run `ct project list`
  to rebuild the index") but never executes them.
- **No remote reporting.** The report stays on the local machine.
- **No replacement for individual commands.** `doctor` aggregates signals; it
  does not re-implement `session stats` or `project list`. When a signal
  points at a problem, the owner drills in with the dedicated command.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks `ok`, no failures in the window. |
| 1 | At least one `warn` check, or at least one failed invocation in the window. |
| 2 | At least one `fail` check (environment is broken). |
| 3 | Invocation log unreadable or corrupt. |

Non-zero exits make `doctor` usable in CI or cron as a health gate, even
though the primary consumer is the interactive owner.
