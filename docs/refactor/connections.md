# Connections and authentication lifecycle

Status: implementation contract. A connection profile is shared by collector and
query commands on every host. It is not itself an authorization grant.

## Configure once

`ct connection configure NAME` stores an endpoint, workspace, optional collector
and project identity, default query source (`local`, `shared`, or compatibility
`auto`), role intent (`collector` or `reader`), and a secret reference. A local-only
profile has no remote endpoint or credential. Profile JSON never contains tokens.
Existing version-2 collector profiles remain readable without rewriting them.

On macOS, interactive configuration stores the token in Keychain. Headless agents
use `--token-env NAME` and their host's secret injection mechanism. Do not accept
tokens as connection command-line arguments or print them in errors/status.
Remote endpoints require HTTPS except explicit loopback development endpoints.

`ct connection status NAME` reports local configuration without a network call.
`ct connection check NAME` authenticates a bounded read-only capability request,
verifies workspace and collector identity, and reports the server's actual roles.
`ct connection rotate NAME` replaces the local secret reference/value. Rotation
must not change collector identity, cursor state, or pending batches.
`ct connection forget NAME` removes only this host's profile and stored credential;
it is not server-side revocation. An owner revokes tokens in the server principal
registry; the next request fails explicitly and pending upload work is retained.
Token issuance and server registry administration remain owner operations; this
change does not invent a self-enrollment or renewal endpoint.

## Resolution and scope

Explicit command profile takes precedence over `CT_CREDENTIAL_PROFILE`, then the
legacy default profile. Complete explicit/environment connection configuration
remains supported. Never silently combine an explicitly selected profile's token
with another endpoint. Query `--source local` requires no cloud credentials and
has no fallback; `--source shared` resolves remote credentials immediately;
compatibility `auto` retains lazy local-first fallback only for documented missing
source conditions. A successful empty local result remains success.

Collector preparation/status/manual service require identity metadata, not a
working token. Publish/automatic service require a collector identity and a usable
credential. The server validates actual `collect` permission and source ownership.
Reader profiles cannot initiate publication. A collector can query shared data
only if its server role set also contains `read` (or owner); client role intent
cannot widen access.

## Error and lifecycle semantics

Configuration missing, credential unavailable, authentication rejected, and
capability denied are distinct bounded states. Status never claims server access
from the presence of a secret. Check never writes a heartbeat, source, or artifact.
A query never invokes upload, including when `CT_AUTO_PUBLISH` was previously set.
Authentication failure leaves local queries/preparation available and remote
batches intact. Retrying after rotation uses the same immutable batch identity.

Browser Access authentication remains distinct from agent bearer authentication.
Both resolve to workspace/resource authorization at the backend. Browser bundles
must never receive collector credentials. No automatic schedule is installed or
enabled during profile configuration, credential checks, rotation, or queries.

## Acceptance

Qualify existing v2 profiles, reader and collector v3 profiles, headless env tokens,
missing/rotated/revoked credentials, mismatched workspace/collector identity,
explicit source precedence, local success without secrets, and zero publication
requests for connection checks and ordinary queries. Keep all fixtures synthetic.
