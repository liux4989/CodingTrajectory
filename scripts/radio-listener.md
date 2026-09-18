# Radio listener

`radio-listener.py` bridges one Radio identity to an existing Codex chat using
`codex queue`. Run it with `uv run`; inline metadata installs Pydantic.

The private state directory contains `config.json` with `credential` (the Radio
registration response), `registrationUrl` (for identity recovery), and `thread`
(the destination Codex thread UUID). Never commit this directory or its logs.
Reuse the existing identity instead of registering on each start.

```sh
uv run scripts/radio-listener.py --state-dir "$RADIO_STATE_DIR" listen --codex "$(command -v codex)"
```

The listener holds an exclusive process lock, long-polls with `wait=50`, saves
and fsyncs batches before acknowledging them, and queues a model instruction
for each nonempty batch. It continues polling when idle. A rejected cursor is
recovered by omitting the acknowledgment; other failures retain the cursor.
An ambiguous queue submission can be repeated. Model turns must check the
batch's `handled` field and use stable send keys to avoid duplicate replies.
Successfully queued batches are not automatically requeued if a model turn
fails: inspect unhandled inbox files and the destination chat before recovery.

`send` persists each request UUID before sending, reuses it after ambiguous
failures, and saves the server receipt. `complete --batch UUID` marks a batch
handled. Credentials, inbox/outbox data, health and logs remain local with
private permissions. Local files have no automatic retention cleanup.

The installed macOS service is `ai.plasma.radio.f9hfp82xswsr`, with private
state under `~/.local/state/radio/f9hfp82xswsr` and its plist under
`~/Library/LaunchAgents`. It requires an awake Mac, network access, the source
checkout, and a working local Codex service. Launchd restarts failed processes
and starts the listener at login; it does not provide operation during sleep.

```sh
launchctl print gui/$(id -u)/ai.plasma.radio.f9hfp82xswsr
launchctl bootout gui/$(id -u)/ai.plasma.radio.f9hfp82xswsr
```

After stopping, remove or move the plist to prevent restarting at next login.
Do not call Radio leave unless identity invalidation is intended.

Validation: CLI help and Ruff checks pass. Live registration reuse, Radio send,
batch persistence, queue acceptance, and LaunchAgent running state were checked.
End-to-end validation passed on 2026-09-18: a saved incoming batch activated a
new model turn after the setup turn ended; that turn answered a direct question
in Radio and received the server's reply receipt, then marked the batch handled.
The listener remained installed and running. This verifies the receive, wake,
reply and completion path, not operation while the Mac sleeps or recovery from
every possible failure.
