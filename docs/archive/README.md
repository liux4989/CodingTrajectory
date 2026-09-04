# Historical designs

These documents preserve prior decisions and attempts. Their original status
claims describe their historical context, not the current implementation.

| Record | Current authority or disposition |
| --- | --- |
| [Remote session ledger](remote-session-ledger-design.md) | Superseded by [control plane](../remote-ct-control-plane-design.md) and [shareable history](../shareable-history.md) |
| [Standalone metrics frontend](metrics-frontend-plugin-design.md) | Retired; comparison features live in Datahub |
| [Step-to-item migration](remove-step-item-migration.md) | Completed hierarchy migration; current hierarchy uses Turn → Item |
| [Item token attribution](item-token-attribution-refactor.md) | Completed refactor; [metrics gate](../metrics-validation-quality-gate.md) owns current validation |
| [June metrics audit](codex-metrics-audit.md) | Dated vendor audit; current committed fixtures remain under validation/metrics |
| [Evaluation high-level design](session-evaluation-high-level-design.md), [full design](session-evaluation-full.md) | Prior evaluation attempt; referenced foundation document and public evaluation implementation are absent from the current tree |

Do not revive old schemas, workers, or compatibility paths from these records.
