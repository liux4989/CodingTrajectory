# Doctor Command

## Goal

`ct doctor` is a single diagnostic surface that combines:

- **Invocation telemetry** - what `ct` has been doing on this machine.
- **Environment health** - whether the CLI is configured correctly and can
  still trust its local cache and telemetry files.

It is the first command the owner should run when something feels off, and the
periodic checkpoint for maintenance.

## Usage

```text
ct doctor [--since DURATION] [--output markdown|json]
```

Defaults to markdown. `--since` accepts `7d`, `30d`, `90d`, `all`; default is
`7d`. `all` is truly unbounded. Unsupported values are rejected at argument
parsing time with a clear error.

## Report sections

### 1. Environment

Verifies the runtime and local files the CLI depends on.

| Check | Source |
|---|---|
| Python version | `sys.version_info` (must be `>= 3.12`) |
| CLI version | package metadata |
| Config files present | `~/.coding-trajectory/config.toml`, `~/.coding-trajectory/index.json` |
| Telemetry enabled | `CT_TELEMETRY` env and `config.toml` `telemetry.enabled` |
| Index cache | `~/.coding-trajectory/index.json`, validated against the persisted cache schema |
| Codex root | `~/.codex/sessions` |
| Claude root | `~/.claude/projects` |
| Pi root | `~/.pi/agent/sessions` |

Each check renders as `ok`, `warn`, or `fail`.

Malformed telemetry configuration, existing vendor paths that are not readable
directories, and malformed cache schemas are `fail`. Missing optional vendor
roots and intentionally disabled telemetry are `warn`.

The index cache check reports:

- Path mapping count.
- Session mapping count.
- File size.
- File age.

Malformed cache JSON or schema mismatches are `fail`, not `warn`, because the
environment is no longer trustworthy.

### 2. Invocation summary

Aggregated statistics over the invocation log for the selected time window.

| Metric | Meaning |
|---|---|
| Total invocations | Count of validated records in the selected window |
| Failed invocations | Count where `ok` is `false` |
| Latency samples | Count of records contributing latency |
| P50 / P95 latency | Computed with linear interpolation across sorted latency samples |
| Top commands | Top 5 by invocation count |
| Top working directories | Top 5 by invocation count |
| Vendors seen | Distinct vendors, with counts |

Latency is accepted only when it is a valid non-negative numeric value.

### 3. Failure summary

All records where `ok` is `false`, grouped by `error` and `cmd`. For each
group:

- Total count in the window.
- Most recent occurrence (timestamp, cwd, session id).
- One sample invocation record.

Markdown is bounded to the first 10 failure groups. JSON remains complete.

### 4. Warning summary

All warning records emitted via `debug.warn`, grouped by `code`. For each code:

- Count of occurrences.
- Severity distribution.
- Top commands that produced the warning.
- Representative message.
- Sample context from the most recent occurrence.

Markdown is bounded to the first 10 warning groups. JSON remains complete.

### 5. Latency trends

Latency trends are grouped by command and time bucket:

- `7d` and `30d`: daily buckets.
- `90d` and `all`: weekly buckets.

Each row reports count, P50, and P95 for that bucket/command pair.

Markdown is bounded to the latest 20 rows. JSON remains complete and includes
the bucket granularity in `latency_trend_granularity`.

### 6. Stale state

Identifies stale cache entries by inspecting only the
`path_to_session_graph` keys in `~/.coding-trajectory/index.json`.

The report includes:

- Total path mappings scanned.
- Total stale path mappings.
- Number of affected session graphs.
- Sample stale entries.

`session_to_session_graph` is not scanned for stale paths because it is a
session-id lookup table, not a filesystem mapping.

Markdown is bounded to the first 20 stale entries. JSON remains complete.

## JSON output

`--output json` emits the full structured report behind the markdown rendering.
The current contract is versioned as `doctor_version = 2`.

Top-level fields:

| Field | Meaning |
|---|---|
| `doctor_version` | JSON contract version |
| `time_window` | Object with `value`, `days`, and `unbounded` |
| `time_window_days` | Convenience field; `null` when unbounded |
| `environment` | Structured environment checks with status/detail and section-specific metadata |
| `invocation_summary` | Aggregated totals, percentiles, top lists |
| `failures` | Full grouped failure list |
| `warnings` | Full grouped warning list |
| `latency_trends` | Full trend rows |
| `latency_trend_granularity` | `daily` or `weekly` |
| `stale_state` | Full stale path entries |
| `stale_state_summary` | Counts for stale path mappings and affected session graphs |

`doctor_version = 2` corrects the earlier contract in four ways:

- `all` is now represented as unbounded instead of a fake large day count.
- The environment section exposes separate vendor roots and separate
  path/session cache mapping counts.
- Stale state only reports filesystem-backed path mappings.
- Markdown sampling is bounded without truncating the JSON payloads.

## Relationship to the invocation log

`ct doctor` is the only CLI command that reads `invocations.jsonl`. It opens
the file read-only, validates each non-empty line with Pydantic, filters the
selected time window, and discards the records after aggregation.

Any of the following make the invocation log unreadable and force exit code `3`:

- Malformed JSON.
- Schema mismatches.
- Invalid timestamps.
- Invalid latency values.

The error includes the file path and line number so the broken record can be
found directly. Invocation logging preserves corrupt lines, including when
recording Doctor's own exit `3`, so a diagnostic run cannot erase the evidence
that caused it to fail.

## Non-goals

- **No interactive mode.** `doctor` prints a report and exits.
- **No auto-fix.** `doctor` surfaces problems but does not repair them.
- **No remote reporting.** The report stays on the local machine.
- **No replacement for dedicated commands.** `doctor` aggregates signals; it
  does not replace the underlying command surfaces.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All environment checks are `ok`, and there are no failed invocations in the window. |
| 1 | At least one `warn` environment check, or at least one failed invocation in the window. |
| 2 | At least one `fail` environment check. |
| 3 | Invocation log unreadable or corrupt. |

Argument parsing failures, such as an unsupported `--since` value, are rejected
by `argparse` before the report is generated and also exit with code `2`.
