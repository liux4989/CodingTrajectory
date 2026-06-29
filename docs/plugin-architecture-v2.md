# Plugin Architecture v2 — Proposal

## Why

The current plugin lifecycle is fragile for both users and plugin authors:

1. **Stale assets after rebuild.** The dashboard server resolves its static
   bundle via `Path(__file__).resolve().parent / "web" / "dist"`. After an
   editable install, `__file__` points at the site-packages copy that hatchling
   baked in via `force-include`, not the source tree. So `npm run build` in
   `packages/plugins/dashboard/web` has no effect until the user re-runs
   `uv pip install -e packages/plugins/dashboard`. The served UI silently
   keeps the old bundle.
2. **Three-step publish flow.** Edit → rebuild → `uv pip install` →
   `ct plugin register-builtins --replace`. Missing one step gives a silently
   broken or stale plugin with no warning.
3. **Absolute-path registry.** `plugins.json` stores absolute manifest paths.
   Reinstalling to a different venv, moving the checkout, or switching Python
   versions silently invalidates every registration.
4. **No auto-discovery.** `pip install ct-plugin-foo` does nothing to `ct`
   until the user manually runs `ct plugin register`. Built-ins only work via
   `register-builtins`, which finds manifests by `Path(__file__).parents[4]`
   — a heuristic that only works inside the dev checkout.
5. **Duplicated plugin plumbing.** Five copies of `_ct_json(...)` across
   plugins, each shelling out to `ct api` and re-ingesting sessions per call.
   No shared SDK, no shared runtime, no shared caching.

The isolation principle (plugins are separate executables that consume
documented `ct` JSON) is sound and should stay. The **install/discovery/asset**
layer on top of it is what needs to be robust.

## Goals

- After `npm run build`, the next `ct plugin dashboard --web` serves the new
  bundle with zero reinstall.
- `pip install ct-plugin-foo` makes `ct plugin foo` available with zero manual
  registration.
- No absolute paths in the registry; registrations survive venv/Python changes.
- One command refreshes the dev environment (`scripts/publish-local.sh`
  collapses to one step; `register-builtins` goes away).
- Plugin authors get a tiny SDK so they stop copy-pasting `_ct_json` and
  `Path(__file__)` asset lookups.

## Non-Goals

- Changing the subprocess-execution model. Plugins remain separate
  executables consuming documented `ct` JSON contracts. We do not load plugin
  Python into the `ct` process.
- Changing the manifest schema's *content* (name, run, requiresCt,
  requiresMethods, tools). Only *how* it is discovered and resolved.
- Adding implicit directory scanning for loose manifests. Explicit packaging
  remains required.

## Design

### 1. Entry-point plugin discovery (replace manual registration)

Each plugin package declares a `ct.plugins` entry point pointing at a small
loader callable inside the package:

```toml
# packages/plugins/dashboard/pyproject.toml
[project.entry-points."ct.plugins"]
dashboard = "ct_plugin_dashboard.plugin:load_plugin"
```

The loader returns a `PluginDescriptor` (dataclass) carrying the manifest dict
plus the package's import root, resolved at call time:

```python
# ct_plugin_dashboard/plugin.py
from importlib.resources import files
def load_plugin():
    manifest = (files("ct_plugin_dashboard") / "ct-plugin.json").read_text("utf-8")
    return PluginDescriptor(
        manifest=json.loads(manifest),
        package="ct_plugin_dashboard",
        entry_module=__name__,  # anchor for asset resolution
    )
```

`ct` collects descriptors by scanning `importlib.metadata.entry_points(group="ct.plugins")`
at startup. This works identically for:

- **editable installs** (dev checkout) — the entry point resolves to the source
  tree;
- **wheel installs** (end users) — resolves to site-packages;
- **the uv workspace** — every `--with-editable packages/plugins/*` shows up
  automatically.

No `ct plugin register`, no `register-builtins`, no `plugins.json` absolute
paths. The registry file is replaced by installed-package metadata, which is
always consistent with the environment.

### 2. Resource-based asset resolution (fix the stale-bundle bug)

Replace every `Path(__file__).resolve().parent / "web" / "dist"` with
`importlib.resources`:

```python
from importlib.resources import files
static_dir = files("ct_plugin_dashboard") / "web" / "dist"
```

`importlib.resources.files()` resolves through the import system, so:

- **editable install** → returns the source-tree path; rebuilding
  `web/dist` in place is immediately visible. **Zero reinstall.**
- **wheel install** → returns the site-packages path where `web/dist` was
  force-included at build time.

This is the single change that makes `npm run build` → `ct plugin dashboard
--web` just work. It also removes the `--static-dir` escape hatch for the
common case.

Keep `force-include` for `web/dist` in `pyproject.toml` so wheel installs
still ship the bundle; the resource API reads it transparently.

### 3. Registry becomes a denylist, not an allowlist

With entry-point auto-discovery, every installed plugin is available by
default. The user-owned registry file (`plugins.json`) shrinks to optional
**overrides only**:

```json
{
  "schemaVersion": 2,
  "disabled": ["review"],
  "aliases": {}
}
```

- `disabled`: opt-out list for users who installed a plugin but do not want it
  routed under `ct plugin`.
- `aliases`: optional name overrides for when two packages claim the same
  namespace.

No absolute paths. Survives venv moves. `ct plugin list` shows discovered +
disabled state. `ct plugin disable NAME` / `enable NAME` replace
`unregister` / `register` for the common case.

`ct plugin register MANIFEST` stays as a **power-user escape hatch** for
unpackaged/local manifests (e.g. a plugin under development that has not been
pip-installed yet), but is no longer required for any packaged plugin.

### 4. Plugin SDK package

Introduce `coding-trajectory-plugin-sdk` (tiny, pydantic-only) that plugins
depend on instead of reimplementing plumbing:

```python
from coding_trajectory_plugin_sdk import (
    PluginDescriptor,           # return from load_plugin()
    ct_json,                    # one subprocess call to `ct api ... --output json`
    ct_json_batch,              # one `ct api batch` call, returns {id: result}
    resolve_package_asset,      # importlib.resources helper + clear error
    which_ct,                   # find the ct executable (CT_COMMAND > PATH > venv)
)
```

This deletes the five copies of `_ct_json` and the `Path(__file__)` asset
snippets. The SDK does **not** import `coding_trajectory` or
`coding_trajectory_cli` — it only shells out and parses JSON, preserving the
isolation boundary.

### 5. One dev command

`scripts/publish-local.sh` becomes:

```bash
uv tool install --force --reinstall \
  --editable packages/cli \
  --with-editable packages/core \
  --with-editable packages/plugins/dashboard \
  ... 
```

…with no `ct plugin register-builtins` step. Entry points auto-register. The
script's only job is to refresh the uv tool environment. Optionally add a
`ct plugin doctor` that verifies every discovered entry point loads and its
`requiresCt` / `requiresMethods` are satisfied, replacing
`scripts/check_packaged_plugins.py`'s ad-hoc validation.

### 6. Manifest stays a packaged resource

`ct-plugin.json` stays as the manifest format, but it is now read via
`importlib.resources` from inside the installed package rather than from an
absolute filesystem path. The `schemaVersion` field is unchanged; the registry
file bumps to `schemaVersion: 2` (denylist shape) while manifests stay at `1`.

## Migration

Each step is independently shippable; no big-bang.

1. **Asset resolution (fixes the reported bug today).** Switch
   `dashboard_web._static_dir` to `importlib.resources.files(...)`. Rebuild
   `web/dist` once. Verify `npm run build` → `ct plugin dashboard --web`
   serves the new bundle without reinstall. ~10 lines.

2. **Plugin SDK extraction.** Move the shared `_ct_json` / `which_ct` /
   `resolve_package_asset` helpers into `coding-trajectory-plugin-sdk` and
   re-point the four plugins at it. No behavior change; pure dedupe.

3. **Entry-point discovery.** Add `[project.entry-points."ct.plugins"]` to
   each plugin pyproject, add the `load_plugin()` callable, and teach
   `plugins.py` to scan entry points first. Keep `register`/`register-builtins`
   working but mark them deprecated. `ct plugin list` shows
   `source: entry-point` vs `source: manual-registry`.

4. **Registry → denylist.** Cut `plugins.json` over to schemaVersion 2
   (`disabled` / `aliases`). Remove `register-builtins` from
   `publish-local.sh`. Delete the `Path(__file__).parents[4]` heuristic.

5. **Cleanup.** Remove the deprecated `register`/`unregister` commands once
   no docs reference them, or keep them as the documented escape hatch for
   unpackaged manifests.

## What stays the same

- Plugins remain separate executables; `ct plugin NAME ...` still dispatches
  via the manifest `run` argv as a subprocess with inherited stdio.
- The manifest schema (`name`, `run`, `requiresCt`, `requiresMethods`,
  `tools`) is unchanged.
- `ct api call` / `ct api batch` / `ct api schema` remain the plugin-facing
  contract surface.
- Core never depends on plugins; plugins never import core or CLI.
- Versioned Pydantic service contracts remain the compatibility boundary.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Entry points require the package to be importable before `ct` lists plugins. | That is already true for any packaged plugin; the entry point only loads the tiny `plugin.py` module, not the full plugin runtime. |
| `importlib.resources` behaves differently across editable backends. | Mandate Python ≥ 3.12 (already required) where `files()` is stable for both PEP 660 editable and wheel installs. Add `ct plugin doctor` to surface resolution failures. |
| Auto-discovery makes a bad plugin hide a good one. | `disabled` denylist + deterministic ordering (alphabetical by entry-point name) + `ct plugin list` showing the resolved source. |
| Users with the old `plugins.json` on upgrade. | Read schemaVersion; v1 registries are migrated to v2 by dropping the now-redundant `plugins` map (every discovered entry point is already available). |

## TL;DR

- Assets via `importlib.resources` → rebuild-and-run with no reinstall.
- Plugins via `ct.plugins` entry points → `pip install` is the only
  registration step.
- Registry becomes a denylist, not an allowlist → no absolute paths, survives
  venv moves.
- A 5-file SDK deletes the duplicated plugin plumbing.
