# Retired legacy reset

The early-development production reset was completed on 2026-09-22. Its temporary access and tooling have been removed. Historical qualification receipts are retained as evidence, not executable procedures.

Routine deployment never deletes data or uploads a replacement dataset. Deploy a tested release with preflight and a short readback check; run data sync separately.

If another explicitly authorized development reset is necessary, pause writes to the exact workspace, delete both its database state and its R2 artifact prefix, verify both are empty, then resume writes. Preserve credentials, bindings, and other workspaces. Do not add a permanent reset endpoint.
