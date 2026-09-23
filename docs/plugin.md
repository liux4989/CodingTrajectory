# Plugin boundary

`ct plugin NAME ...` runs a separately packaged command extension. Manifests at
`packages/plugins/*/plugin.toml` declare `name`, `description`, `entry`, required
Core method versions, and descriptive `tools`. `CT_PLUGIN_DIR` can select another
plugin root. `ct plugin list` shows discovery and compatibility errors.

The CLI checks required method versions, then starts the plugin entry script in
the plugin directory with its Python import path set. The plugin owns arguments,
help, dependencies, exit status, and service lifecycle. `uv sync --all-packages`
installs workspace dependencies; `scripts/publish-local.sh` installs an editable
CLI linked to this checkout. It does not publish a remote package.

Plugins consume public Core contracts through `ServiceRuntime`, `PluginApiClient`,
or `ct api call/batch`; they never reconstruct canonical resources independently,
import a privileged projection facade, or redefine native metric formulas.
There is no first-party exception to this ownership boundary.

```bash
ct api schema session.items
ct api call session.items --params '{"session_id":"SESSION_ID","limit":20}'
ct api call session.summary --params '{"session_id":"SESSION_ID"}'
ct plugin loop web --help
```

## CodingTrajectory Loop

Loop is the local-first Analytics plugin under `packages/plugins/loop`. Its
React/shadcn browser composes frozen Core methods, while its Python service owns
only local delivery and strict Pydantic investigation view state. It uses a local
runtime with no remote fallback regardless of the user's default query source.

```bash
bun install --cwd packages/plugins/loop/web --frozen-lockfile
bun run --cwd packages/plugins/loop/web build
uv run ct plugin loop web
```

Explore discovers projects and sessions. Investigations orient with summary and
overview, progressively expand canonical items and events, and copy stable
references. SQLite stores only references and titles under `ct.loop.v1`.
The service exposes no cleanup, deletion of logs, publication, or hosted route.
Monitor and Improve remain separate later workflows, without fake navigation.

See [Loop design](loop-design.md) for exact routes, local security assumptions,
coverage semantics, commands, and the non-blocking inventory pagination proposal.
The [Core freeze](core-protocol.md) governs all 18 Core methods and Chronicle.

## Session breakdown

The read-only `breakdown` plugin presents one local session's native tool-call
sequence, counts by Read / Write / Edit / Bash / Other, visible tool-token
estimates, and the semantic context composition already provided by
`session.stats`. It consumes the versioned `session.tool_usage` and
`session.stats` contracts; it does not infer an exact billed-token split from
command wrappers. Unknown tool names remain **Other**, and a multi-command
`exec` wrapper remains one call. Tool-token estimates are not billed usage or
the active resident context total. The session is selected when launching the
server; the browser cannot query other session IDs.

```bash
uv run --package coding-trajectory ct plugin breakdown web SESSION_ID
```

The server listens on the local loopback interface (default port 8766). The
plugin has no persistent state or write routes; use `--port` to run alongside
Loop. A trusted authenticated proxy can be permitted with `--allow-host`.
