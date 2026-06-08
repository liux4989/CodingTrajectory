# CLI Refactor: file-per-command-group + per-command renderers

## Motivation

The CLI lives in a single 1269-line module,
[`packages/cli/src/coding_trajectory_cli/cli.py`](../packages/cli/src/coding_trajectory_cli/cli.py).
It mixes parser construction, parameter assembly, markdown rendering, plugin
glue, and dispatch in one file. Adding a command requires editing several
scattered regions and a central rendering switch.

Inspiration: the [Polymarket CLI](https://github.com/Polymarket/polymarket-cli)
organizes one module per command group, each exposing a uniform registration
hook, and attaches the renderer to the command instead of a central switch.
The goal: **adding a command touches exactly one file.**

This refactor is **behavior-preserving**: `ct` output stays byte-identical and
the public `coding_trajectory_cli.cli:main` entry point is unchanged.

## What we keep

The current design already follows good patterns; do not change these:

- **Method-string dispatch** via `set_defaults(_method=..., _params=...)` into
  `coding_trajectory.service.dispatch(method, params)`. The service layer is the
  business seam and stays untouched.
- **Two-mode output** (`markdown` | `json`) with a per-command `_default_output`
  resolved by `_selected_output(args)`.
- **Format-aware errors** — JSON `{"error": {"message": ...}}` to stderr.
- **No credential/config waterfall.** The CLI has no secrets; we deliberately do
  *not* adopt Polymarket's flag→env→config-file auth resolution.

## Target layout

```
coding_trajectory_cli/
├── cli.py              # root parser, main(), dispatch loop, error handling
├── _shared.py          # reusable flag adders, param helpers, _json_text, formatters
├── commands/
│   ├── __init__.py     # REGISTRARS list
│   ├── project.py      # register(subparsers) + param builders + renderers
│   ├── session.py
│   └── plugin.py
└── render/             # (optional) markdown renderers if commands/*.py grows large
    ├── project.py
    └── session.py
```

Each `commands/*.py` exposes a single hook:

```python
def register(subparsers) -> None:
    ...
```

`cli.py` discovers and calls every registrar:

```python
from coding_trajectory_cli.commands import REGISTRARS

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ct", formatter_class=_GhFormatter)
    _add_output_flags(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for register in REGISTRARS:
        register(subparsers)
    return parser
```

## Change 1 — split per command group

Move each top-level group block out of `cli.py` into its own module.

- `project list`, `project sessions` → `commands/project.py`
- `session overview|stats|usage|step|event|scan` → `commands/session.py`
- `plugin list` + dynamic plugin commands + `_dispatch_plugin_argv` →
  `commands/plugin.py`

Shared helpers (`_add_output_flags`, `_add_base_output_flags`,
`_add_params_flag`, `_add_agent_vendor_flag`, `_add_metrics_flags`,
`_params_from_json`, `_json_text`, `_short_id`, `_positive_int`,
`_json_object_arg`, `_GhFormatter`, `_OUTPUT_CHOICES`) move to `_shared.py`.

## Change 2 — attach renderer to the command

Today `_render_payload` is a central `if/elif` chain keyed on `args._method`
([cli.py L1216](../packages/cli/src/coding_trajectory_cli/cli.py#L1216)). The
plugin path already proves the better pattern by attaching `_render_payload` in
`set_defaults`. Generalize it for all commands using a `_renderer` callable.

Each subcommand registers its own renderer:

```python
# commands/project.py
project_list.set_defaults(
    _method="project.list",
    _params=_project_list_params,
    _default_output="markdown",
    _renderer=_render_project_list_markdown,   # callable(payload) -> str
)
```

`cli.py` collapses to a single generic function:

```python
def _render_payload(args, payload):
    plugin_renderer = getattr(args, "_render_payload", None)   # legacy plugin hook
    if callable(plugin_renderer):
        return plugin_renderer(args, payload)
    if _selected_output(args) == "json":
        return _json_text(_compact_payload(args._method, payload))
    renderer = getattr(args, "_renderer", None)
    if renderer is not None:
        return renderer(payload)
    return _json_text(_compact_payload(args._method, payload))
```

Result: no central switch to edit when adding a command. Commands without a
markdown renderer fall through to compact JSON exactly as today.

## Change 3 (optional, low priority) — reduce param-builder boilerplate

The `_*_params(args)` functions are mostly mechanical "copy attr → dict if not
None". A small shared helper keyed on a field list can remove the repetition:

```python
def collect(args, fields, base=None):
    params = dict(base or _params_from_json(args))
    for name in fields:
        value = getattr(args, name, None)
        if value is not None:
            params[name] = value
    return params
```

Keep the few builders that have real logic (e.g. `_project_sessions_params`'s
`--all-time` / `--since-days` defaulting) as-is. Do not over-abstract.

## Migration steps

1. Create `_shared.py`; move shared helpers and constants. Re-export from `cli.py`
   if anything external imports them (check first).
2. Create `commands/project.py`, `commands/session.py`, `commands/plugin.py`,
   each with `register(subparsers)` plus its param builders and renderers.
3. Add `commands/__init__.py` with `REGISTRARS = [project.register,
   session.register, plugin.register]`.
4. Replace the inline parser blocks in `cli.py` with the registrar loop.
5. Generalize `_render_payload` to use `_renderer` (Change 2).
6. Delete the now-dead per-method `if/elif` rendering branches.
7. Keep `main()`, `_dispatch`, and the plugin argv fast-path wiring in `cli.py`.

## Verification

Behavior-preserving, so verify with output diffs rather than new tests
(`AGENTS.md`: do not write unit tests).

- Run existing CLI tests / `pytest`.
- Capture before/after output for representative commands and diff:
  - `ct project list`
  - `ct project sessions`
  - `ct session overview` (markdown and `-o json`)
  - `ct session stats`, `ct session usage`
  - `ct plugin list` and a real plugin invocation
  - error paths (unknown session id) — confirm stderr JSON unchanged
- Confirm `ct --help` and every subcommand `--help` page is unchanged.

## Out of scope

- Service / business logic (`coding_trajectory.service`, `query`, analysis).
- Output format set (still `markdown` | `json`).
- Adopting credential/config resolution from Polymarket.
- Switching argument-parsing libraries (stay on `argparse`).
```
