---
schema: ct.release.v1
release_id: 0
target: staging
title: Initial batch-release baseline
---

# Batch release

`release_id` controls CI release-candidate preparation, not automatic activation.
Ordinary commits and note edits run normal CI without preparing a release.

When a reviewed batch is ready, advance `release_id` by exactly one in its final
commit. That commit can also select `staging` or `production` and update the title.
Do not decrease or skip IDs. A target change requires an ID increment.

Release `0` establishes the baseline and never deploys. Activation requires
explicit authorization and the [release procedure](docs/operations.md#prepare-and-deploy-a-release).

## Pending changes

- Add reviewed changes while assembling the batch.
