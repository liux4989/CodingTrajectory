---
schema: ct.release.v1
release_id: 0
target: staging
title: Initial batch-release baseline
---

# Batch release

`release_id` is the only deployment trigger. Ordinary commits and edits to these
notes continue through normal CI without preparing a release. When a reviewed
batch is ready, change `release_id` by exactly one in the final commit. The same
commit may select `staging` or `production` and update the title and notes.

Release `0` establishes the baseline and never deploys.

## Pending changes

- Add changes here while the batch is being assembled.
