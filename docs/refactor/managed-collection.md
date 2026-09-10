# Managed collector service

Implemented locally on 2026-09-10. Host schedule enablement is an explicit operator
action; the existing Mac schedule has not been activated by this implementation.

## Register and install once

Configure a collector connection as described in [operations](operations.md), then
register its project and generate the supervisor template:

```sh
ct collector service add coding --profile workstation \
  --directory /path/to/CodingTrajectory
ct collector service install
ct collector service status
```

The profile supplies the project name, workspace, collector identity and optional
delivery-state path. `add` accepts `--project-name` and `--state-path` for explicit
overrides. Register multiple projects with distinct names and state paths. Existing
pending batches and publication policy are preserved; a new state starts manual.

Configuration lives in `~/.coding-trajectory/control-plane/service/service.json`.
The configuration pins project directories and delivery paths, references profiles
in the configured connection directory, and contains no credentials. One service
runs preparation and delivery in two independent lanes and services all registered
projects. A failed project does not stop another project's work.

## Enable and operate

```sh
ct collector service enable --automatic
ct collector service status
ct collector service pause --project coding
ct collector service resume --project coding
ct collector service policy --mode manual --project coding
ct collector service disable
```

`install` only writes a template in the private service directory. `enable` copies
the template into launchd's user LaunchAgents directory on macOS or systemd's user
unit directory on Linux and starts supervision. Without `--automatic`, enablement
retains each project's existing publication policy. Pause state is always retained.
The service resumes after a process failure and when the user's supervisor starts.
Headless Linux boot before user login requires the host's user-manager/lingering
configuration; this command does not change that system policy.

`disable` stops/removes the registered supervisor unit while retaining templates,
credentials and all delivery state. Stop the service before `remove NAME`; removal
only forgets that project's registration. `run` executes in the foreground for
diagnosis or use with another host supervisor. A second host service is rejected.

## Headless credentials and recovery

macOS Keychain references work under the same user account. Environment references
must be injected into the supervisor environment, or use an explicitly supplied
private JSON file:

```sh
ct collector service install --secret-file /private/path/collector-secrets.json
```

Its keys are the token environment-variable names from connection profiles and
its values are their secrets. It must be owned by the service user, have no group
or other permissions, and remain outside version control. The service reloads the
selected value for each delivery cycle, so rotation needs no outbox reset. Secret
values are never included in supervisor templates, status or failure output.

Status makes no network requests. It reports all project queues, publication and
pause policy, process heartbeat, last preparation/delivery progress and bounded
error remedies. Network failures retain the same attempted batches; authorization,
ownership or restore errors retain work for the existing explicit connection and
reconciliation commands. Do not remove SQLite files to recover a service.

## Qualification

`uv run python scripts/qualify-managed-collection.py` passed 22 synthetic CLI,
SQLite, HTTP-outage and process-lifecycle checks, including a hard restart,
unchanged consumed cursors, project failure isolation, secret-file rotation and
retained pause/manual policy. Both platform templates are inspected. This check
does not register an OS service, and does not by itself prove live multi-host
publication or remote restore recovery.
