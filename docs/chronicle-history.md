# Chronicle historical facts contract

`ct.published_facts.v1` is the sole bounded historical publication contract. It
is derived directly from one canonical `SessionGraph`; it is not a raw-sharing
format or a second canonical graph model.

## Stable publication seam

`published_fact_set_for_store(DocumentStore)` iterates canonical graphs and calls
`build_published_fact_set(SessionGraph)`. Publication never parses raw
source records or repairs deduplication/accounting. Missing canonical coverage is
reported as unavailable rather than replaced with zero.

`session_graph_from_fact_set(PublishedFactSet)` performs the direct inverse for
shared handlers. It validates identities, ownership, ordering, references,
hashes, cardinality, privacy policy, and encoded bounds before reconstruction.

Published facts retain bounded identity, topology, ordering, lifecycle,
measurement, request/model/runtime, normalized event-envelope, and tool-evidence
fields. Tool evidence is an allowlist projection from canonical items and exact
measurements. Commands retain only an allowlisted executable name (or the generic
`command` fallback), never arguments. Unknown tools fail closed.

Excluded data includes raw records, occurrence inventories, transcript bodies,
full prompts/reasoning, raw tool input/output, arbitrary event/vendor payloads,
secrets, media/blob bodies, and host-absolute paths. Safe derived IDs may identify
published relationships without exposing source payloads.

## Identity and bounds

Every payload identity and reference must agree with its row `fact_id`,
`parent_id`, graph ownership, and another retained row where required. The client
validates a complete `PublishedFactSet`; Cloudflare repeats the same integrity
checks before commit. Hash/digest recomputation cannot legitimize an inconsistent
set. A topology `spawn_origin.target_session_id` may name an observed child that
is outside the retained graph; it is an explicit external topology reference.
Published edges, and every turn/item/event origin they carry, require retained
owned rows.

One row is limited to 512 KiB, one graph to 8 MiB, and one atomic publication to
16 MiB. Reads are deterministic pages bounded by both 2,048 rows and 1 MiB. A
bound failure rejects the operation rather than trimming silently.

Historical methods have one response shape. Graph methods require
`root_session_id`; session methods require `session_id`; `turn_id` is subordinate.
Paging uses stable cursors or `before_turn_id` plus `limit`. One absolute protocol
timestamp represents `modified_since`; CLI convenience durations are translated
before dispatch.

## Qualification scope

The historical OSO replay receipt applies only to ingestion commit
`1f3e86cab69c931a2580fa0ec6de00f71ad99ba4` and its exact source pair. It found
6,706,498 processed child tokens and 17,453,805 graph tokens, excluding 240,402
inherited tokens, while preserving 476 parent and 319 child occurrences and a
24-segment parent union delivered to 11 children. One copied untagged metadata
occurrence remained preserved but non-projecting. This is not proof of universally
complete ownership classification or a replay against later publication heads.
