# Comparanda Tests

The tests here cover the reusable inner-loop methodology contracts.

| Test file | Contract covered |
|---|---|
| `test_extraction_sdk.py` | SDK row construction, chunking, inherited fields, validation, and finalization behavior. |
| `test_run_extraction_build.py` | Controlled execution environment for extractor-authored build scripts. |
| `test_validate_extraction_write.py` | Hook policy that blocks direct extraction writes and permits sanctioned finalizers. |
| `test_extractor_session_registry.py` | Extractor session metadata used for QC repair/resume. |
| `test_normalization_api.py` | Normalizer write API, apply flow, and validation. |
| `test_normalization_reconcile.py` | Deterministic stale-entry/reconciliation hint generation. |
| `test_observation_log.py` | Structured observation-log append/query behavior. |
| `test_orchestrator_docs.py` | Orchestrator/prompt contract checks for repair, registry, SDK, and QC action fields. |

Run from `comparanda/`:

```bash
uv run --with pytest --with pymupdf pytest tests
```
