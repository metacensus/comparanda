# Normalization API Agent Contract

This is the stable contract for Comparanda normalizer agents.

Use `scripts/normalization_api.py` for all normalization decisions,
normalization-table writes, canonical-field propagation, and validation. Do not
edit normalization JSON files or extraction JSONL files directly.

Only read `scripts/normalization_api.py` internals if:

- this contract lacks a capability needed for the current normalization task;
- the CLI reports an internally inconsistent validation result;
- a traceback points to an API bug.

If you inspect internals, record why in your final normalizer summary.

Do not inspect API internals merely to prepare a final report, confirm command
names, or summarize implementation details. Use this contract and CLI `--help`
for that.

## Core Boundary

Extraction rows contain facts and current canonical fields only. Source/raw
extracted fields are immutable after extraction. Rows must not contain
reconciliation history, prior mappings, review comments, or audit text.

Normalization tables are compact lookup tables. They contain current mapping
state only.

Verbose reconciliation rationale belongs in:

```bash
projects/{domain}/normalization/reconciliation/decisions_iter_{N}.jsonl
```

## Required Workflow

1. Read the latest schema and discover normalizable fields from
   `normalization:` annotations.
2. Run the reconciliation hint helper:

   ```bash
   uv run scripts/normalization_reconcile.py \
     --project projects/{domain} \
     --iteration {N}
   ```

3. Review `projects/{domain}/normalization/reconciliation_candidates_iter_{N}.json`.
   The top-level `candidates` list is the primary schema-reconciliation queue.
   `appendix_candidates` are low-confidence/null/catch-all entries from
   schema-unaffected tables; review them only when explicitly instructed or when
   they are directly relevant.
4. For every reviewed candidate, record the decision through
   `normalization_api.py reconcile`.
5. Propagate approved canonical mappings through `normalization_api.py apply`.
6. Run `normalization_api.py validate` before finishing.
7. Append only a concise observation-log summary with
   `scripts/observation_log.py append`. Omit the `id` field and let the helper
   allocate the next collision-safe `OBS-NNN` id.

## Commands

### Record One Decision

```bash
uv run scripts/normalization_api.py reconcile \
  --project projects/{domain} \
  --iteration {N} \
  --table {table_name} \
  --raw '{raw value}' \
  --disposition revise_mapping \
  --canonical-id '{canonical id}' \
  --confidence 0.92 \
  --reason 'Short semantic reason.' \
  --evidence 'projects/{domain}/data/processed/{paper}/sections/results.md'
```

Valid dispositions:

- `revise_mapping`: change the current canonical mapping.
- `reuse_unchanged`: current mapping is still correct under the latest schema.
- `keep_disputed`: value remains unresolved or disputed.
- `defer_with_reason`: additional source material or external lookup is needed.

`revise_mapping` requires `--canonical-id` and `--confidence`.

Non-revision dispositions must not supply `--canonical-id` or `--confidence`.
Use the decision ledger for reasoning, not table-entry audit fields.

Use `--canonical-id __null__` only when the current mapping should explicitly
be null/unresolved.

### Apply Canonical Fields

```bash
uv run scripts/normalization_api.py apply \
  --project projects/{domain} \
  --threshold 0.9
```

`--threshold` values below `0.9` require `--allow-reviewed-medium` and should
only be used for explicitly reviewed medium-confidence reconciliation decisions:

```bash
uv run scripts/normalization_api.py apply \
  --project projects/{domain} \
  --tables {table_name} \
  --raw '{raw value}' \
  --threshold 0.85 \
  --allow-reviewed-medium
```

Optional narrowing:

```bash
uv run scripts/normalization_api.py apply \
  --project projects/{domain} \
  --tables comparison_groups \
  --raw 'wild-type'
```

The apply command discovers source/canonical companion field pairs from schema
annotations and extraction guidance. It is domain-agnostic and does not contain
single-domain rules or assumptions about one naming convention.

### Validate Writes

```bash
uv run scripts/normalization_api.py validate \
  --project projects/{domain} \
  --base-ref HEAD
```

Validation checks:

- extraction JSONL parses;
- row counts are unchanged relative to `--base-ref`;
- no source/raw extracted field changed;
- only schema-normalized canonical fields changed;
- normalization table entries do not contain verbose audit keys;
- reconciliation ledgers parse and use valid dispositions.

### Clean Verbose Audit Metadata

Use only for migration cleanup when an older run wrote verbose audit metadata
into normalization tables:

```bash
uv run scripts/normalization_api.py clean-audit-metadata \
  --project projects/{domain} \
  --iteration {N}
```

When `--iteration` is supplied, stripped verbose metadata is first migrated into
the reconciliation decision ledger for that iteration.

## Prohibited Behavior

- Do not write Python/heredoc scripts that mutate normalization JSON.
- Do not directly rewrite extraction JSONL.
- Do not store `previous_mappings`, `reconciliation_reason`, or long audit text
  in normalization table entries.
- Do not read, grep, or sed `scripts/normalization_api.py` internals during an
  ordinary normalizer run unless the contract's explicit exception criteria are
  met.
- Do not hand-author observation ids. Omit `id` in observation append payloads
  unless a user explicitly instructs a specific id.
- Do not use gold-standard data for normalization.
- Do not run extraction, extraction QC, schema proposal, MA critique, eval,
  portal export, or outer-loop calibration from a normalizer-only run.

## Final Summary

Report:

- reconciliation helper candidate counts;
- decision counts by disposition;
- API commands used;
- apply summary;
- validate result;
- any deferred/disputed values;
- token/cost summary when available.
