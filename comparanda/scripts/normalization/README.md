# Normalization Scripts

The normalizer agent owns normalization table maintenance. These scripts keep
the agent's writes compact, auditable, and current across schema iterations.

| Script | Use |
|---|---|
| `normalization_reconcile.py` | Generate candidate stale/conflict hints after schema or guidance changes. |
| `normalization_api.py reconcile` | Record LLM normalization decisions and ledger history. |
| `normalization_api.py apply` | Propagate approved canonical mappings into extraction rows. |
| `normalization_api.py validate` | Check normalization table and extraction consistency. |
| `normalization_api.py clean-audit-metadata` | Remove verbose audit-only material from compact current-state tables when needed. |

The reconcile hints are not truth; the normalizer agent must still make the
semantic decision.
