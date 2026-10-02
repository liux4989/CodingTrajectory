# Radio listener

`radio-listener.py` connects one Radio identity to an existing Codex chat through
`codex queue`. Run it with `uv run`; inline metadata installs Pydantic.

**Warning:** keep the state directory and logs private and outside Git.
Its `config.json` contains `credential`, `registrationUrl`, and the destination
`thread` UUID. Reuse that identity; do not register again on each start.

```sh
uv run scripts/radio-listener.py --state-dir "$RADIO_STATE_DIR" \
  listen --codex "$(command -v codex)"
```

The listener holds an exclusive process lock and continues polling when idle.
It persists batches before acknowledging them and queues each nonempty batch.
A rejected cursor omits acknowledgment for recovery; other failures retain the cursor.

Model turns must check `handled` and use stable send keys to prevent duplicate
replies after an ambiguous queue result. Failed model turns are not automatically
requeued. Inspect unhandled inbox files and the destination chat before recovery.

`send` persists its request UUID before sending and reuses it after ambiguous
failures. `complete --batch UUID` marks a batch handled. Local state has private
permissions but no automatic retention cleanup.

## macOS service

The historical installation uses `ai.plasma.radio.f9hfp82xswsr`, state under
`~/.local/state/radio/f9hfp82xswsr`, and a plist under `~/Library/LaunchAgents`.
Check current service state before assuming it is running:

```sh
launchctl print gui/$(id -u)/ai.plasma.radio.f9hfp82xswsr
```

To stop that installation:

```sh
launchctl bootout gui/$(id -u)/ai.plasma.radio.f9hfp82xswsr
```

Move or remove its plist to prevent restart at login. Do not call Radio leave
unless you intend to invalidate the identity.
The service needs an awake Mac, network access, the checkout, and a working
Codex service. Launchd restart does not provide operation while the Mac sleeps.
