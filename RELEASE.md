---
schema: ct.release.v1
release_id: 0
target: staging
title: Initial batch-release baseline
---

# Batch release

`release_id` records a reviewed batch. Core CI validates the marker transition;
it does not prepare candidates or deploy the parked remote runtime.

When a reviewed batch is ready, advance `release_id` by exactly one in its final
commit. That commit can also select `staging` or `production` and update the title.
Do not decrease or skip IDs. A target change requires an ID increment.

Release `0` establishes the baseline. The marker is not deployment authorization;
remote preparation and activation are [unsupported](docs/operations.md#release-marker-validation).

## Pending changes

- Add reviewed changes while assembling the batch.
