# Extraction SDK Agent Contract

This is the stable extraction surface for extractor agents. Use this document
before reading SDK or validator internals. Read implementation files only when
this contract is missing a needed capability, a traceback points to an SDK bug,
or validator output is internally inconsistent. If you read internals, say why
in your final extraction summary.

## Goal

Produce `<project-root>/extractions/<paper_id>.jsonl` only through the
staged Extraction Builder SDK or `scripts/extraction/write_extraction.py`. Never write the
final JSONL directly.

Use one build script per paper:

```text
<project-root>/extractions/.staging/<paper_id>/scripts/build.py
```

On later schema iterations or QC repairs, modify that existing build script
instead of rebuilding from scratch unless the orchestrator explicitly requests a
full replacement extraction.

## Blessed Runner

Run build scripts from the repository root through:

```bash
uv run scripts/extraction/run_extraction_build.py \
  --paper-id <paper_id> \
  --project <project-root> \
  --script <project-root>/extractions/.staging/<paper_id>/scripts/build.py
```

The runner sets the import path, working directory, and useful environment
variables:

- `COMPARANDA_PROJECT_DIR`
- `COMPARANDA_PAPER_ID`
- `COMPARANDA_SCHEMA_PATH`
- `COMPARANDA_BUNDLE_DIR`
- `COMPARANDA_STAGING_DIR`

If the build script writes staging files but does not finalize, add
`--finalize` to finalize through `scripts/extraction/extraction_builder.py finalize`.

## Minimal Build Script

```python
from scripts.extraction.extraction_sdk import (
    ExtractionBuilder,
    missing,
    derived,
    inferred,
    not_applicable,
    paper,
    source_component,
)

paper_id = "paper_1"
b = ExtractionBuilder(
    project_dir="<project-root>",
    paper_id=paper_id,
    schema_path="<project-root>/schema/v001.yaml",
    bundle_dir=f"<project-root>/data/processed/{paper_id}",
)
E = b.evidence

study_quote = E.quote("sections/results.md").exact(
    "Treatment group A had response in 3 of 10 participants."
)

b.common_fields({
    "pmid": paper(12345678, E.metadata("paper_id")),
})

with b.chunk("primary_results") as c:
    c.add_row({
        "entity_raw": paper("Treatment group A", study_quote),
        "outcome_raw": paper("response", study_quote),
        "comparison_group_raw": paper("group A", study_quote),
        "n_total": paper(10, study_quote),
    })
    failures = c.validate()
    if failures:
        raise SystemExit("fix chunk validation before continuing")

b.meta({
    "sources_used": [{"source": "sections/results.md", "role": "direct"}],
    "identity_dimension_values": {"entity_raw": ["Treatment group A"]},
})

b.finalize(f"<project-root>/extractions/{paper_id}.jsonl")
```

## Common Examples

### Simple 2x2 Group-Vs-Reference Counts

Use one row for the focal group and one row for the reference group.
Do not leave the reference group implicit.

```python
treated = E.table_cell("tables/table_2.md", row="Treatment group A", column="Response")
reference = E.table_cell("tables/table_2.md", row="Reference group", column="Response")

with b.chunk("table_2_2x2") as c:
    c.add_row({
        "entity_raw": paper("Treatment group A", treated),
        "comparison_group_raw": paper("Treatment group A", treated),
        "outcome_raw": paper("Response", treated),
        "n_events": paper(3, treated),
        "n_total": paper(10, treated),
    })
    c.add_row({
        "entity_raw": paper("Reference group", reference),
        "comparison_group_raw": paper("Reference group", reference),
        "outcome_raw": paper("Response", reference),
        "n_events": paper(2, reference),
        "n_total": paper(90, reference),
    })
```

### Continuous Or Model-Based Estimate Without Group Counts

If a table reports a model estimate without group count rows, do not invent
denominators. Extract the reported regression row with the schema's comparison
or routing fields and leave unavailable group-specific counts plain null.

```python
model = E.table_cell("tables/table_2.md", row="Model-adjusted estimate", column="Effect")
c.add_row({
    "entity_raw": paper("Model-adjusted estimate", model),
    "comparison_type": derived(
        "model_based_estimate",
        model,
        derivation="Schema routing sentinel for a model-based estimate row.",
        reasoning="The cited table row reports a model estimate, not separate group-specific event counts.",
    ),
    "comparison_group_raw": derived(
        "not_applicable",
        model,
        derivation="No discrete comparison group is reported for the model estimate.",
        reasoning="The model reports one adjusted estimate rather than separate table strata.",
    ),
    "effect_size": paper(1.42, model),
    "effect_size_type": derived(
        "adjusted_model_estimate",
        model,
        derivation="Schema effect-size type for the cited model estimate.",
        reasoning="The row label identifies the estimate as adjusted/model-based.",
    ),
    "n_events": missing("not_found", "The table reports a model estimate, not group-specific event counts."),
})
```

### Scope-Only Comparison

When the paper compares a scope/category stratum and no entity is part of the
row identity, use the schema sentinel instead of fabricating an entity.

```python
scope = E.table_cell("tables/table_4.md", row="High-risk subgroup", column="Response")
c.add_row({
    "entity_raw": not_applicable(
        "This is a subgroup-only comparison; no entity defines the row.",
        scope,
    ),
    "comparison_group_raw": paper("High-risk subgroup", scope),
    "outcome_raw": paper("Response", scope),
})
```

### Continuous Outcome Without Mean/SD Fields

If the current schema has no field for a reported continuous outcome, preserve
the fact as a schema gap in meta-commentary instead of squeezing it into an
incompatible count/effect field.

```python
b.meta({
    "schema_vocabulary_gaps": [{
        "field_need": "continuous outcome value",
        "paper_value": "Median score by subgroup",
        "evidence": "tables/table_3.md reports medians and ranges, not event counts.",
        "recommendation": "Add continuous-outcome fields before this value can be represented losslessly.",
    }]
})
```

### Sensitivity / Specificity / PPV / NPV Cells

Diagnostic-performance cells are high leverage; cite each cell directly.

```python
sensitivity = E.table_cell("tables/table_5.md", row="Screening rule", column="Sensitivity")
ppv = E.table_cell("tables/table_5.md", row="Screening rule", column="PPV")
c.add_row({
    "outcome_raw": paper("screening diagnostic performance", sensitivity),
    "sensitivity": paper(25, sensitivity),
    "positive_predictive_value": paper(100, ppv),
})
```

## Evidence Helpers

Use SDK evidence handles rather than hand-built source dicts.

```python
E.quote("sections/results.md").exact("verbatim sentence from the bundle")
E.quote("sections/results.md").find("short text or regex")
E.quote("sections/results.md").slice_between("start phrase", "end phrase")
E.table_row("tables/table_2.md", row="Treatment group A")
E.table_cell("tables/table_2.md", row="Treatment group A", column="Cases")
E.figure("figures/figure_1.png").label("visible figure value")
E.metadata("paper_id")
```

For tables, prefer the source path that gives the validator a real table grid.
When using table evidence, include row and column anchors with `E.table_cell`
for high-leverage values.

## Field Constructors

- `paper(value, source)`: the value appears directly in the cited quote or
  anchored table row.
- `derived(value, sources, derivation=..., reasoning=...)`: the value is
  calculated, composed, or a schema sentinel supported by cited sources.
- `inferred(value, sources, reasoning=...)`: a non-verbatim judgment from a
  cited source, usually figure-derived or ambiguous.
- `missing(reason, explanation, sources=...)`: record an absent value with `missing_reason`, nonempty `reasoning`, and evidence supporting that reason where available. Reasons: `not_found`, `explicitly_unavailable`, `referenced_source_unavailable`, `definition_mismatch`, `unreadable`, `conflicting_evidence`, `not_applicable`. Explicitly unavailable combines source-stated non-reporting and non-measurement; explain which occurred.
- `missing(reason=None, explanation=...)`: uncategorized fallback only when no listed reason applies. Explain the specific situation and why it is uncovered. Validation reports every use for QC review. Empty explanations are invalid.
- `null(...)` and `abstained(...)` are not supported authoring alternatives in the new contract. Historical files remain readable without guessing a category; no new output may use these helpers to bypass missingness metadata.
- `not_applicable(reasoning, sources)`: the schema sentinel is appropriate for
  this row.

`derived` is not an escape hatch. If the field value or operand is not in the
parent quote, cite the component quote that does contain it.

## Derived Arithmetic

Every arithmetic derivation must have `source_components`, including prose
arithmetic such as "3 events plus 7 non-events gives the denominator 10." Do
not avoid this requirement by writing the calculation as prose instead of a
formula.

```python
event_quote = E.quote("sections/results.md").exact(
    "Treatment group A had response in 3 of 10 participants."
)
nonevent_quote = E.quote("sections/results.md").exact(
    "The complementary non-event count was 7 participants."
)

n_total = derived(
    10,
    [event_quote, nonevent_quote],
    derivation="3 + 7 = 10",
    reasoning="Events plus non-events form the group denominator.",
    source_components=[
        source_component("events", 3, event_quote),
        source_component("non_events", 7, nonevent_quote),
    ],
)
```

The validator checks that:

- arithmetic evaluates to the field value;
- each numeric operand is represented by a component value;
- each component has sources;
- each component value appears in its own cited quote or anchored table row.

## Chunking Pattern

For large papers, organize the build script as:

```python
def add_table_2_rows(b):
    E = b.evidence
    with b.chunk("table_2_primary_counts") as c:
        ...
        failures = c.validate()
        if failures:
            raise SystemExit("table_2_primary_counts failed validation")

def add_table_s6_rows(b):
    ...

add_table_2_rows(b)
add_table_s6_rows(b)
b.finalize(...)
```

This keeps validator failures local to the source table and avoids repairing a
large monolithic payload at the end.

## Validator Failure Meanings

Validation output is intentionally compact. Repair from the grouped clusters
first; open the machine-readable validation JSON or staged rows JSON only when
the cluster does not give enough row/field/source context.

The compact report includes:

- `Rows file`: the staged rows payload whose zero-based indexes are shown in
  the clusters.
- `Machine report`: the grouped JSON report for targeted inspection.
- `hint=<CODE>`: a stable hint code. Definitions are below so reports do not
  repeat long guidance on every failure.

Hint codes:

- `PROV_LITERAL`: `paper(...)` means the field value must appear literally in the cited quote or anchored table cell. If the value is not literal, either cite a source that contains it, use `missing(...)` with the applicable reason when no supported value can be assigned to this field, use `derived(...)` for a non-null schema label, sentinel, composed label, or calculated value supported by sources, use `inferred(...)` for a contextual judgment that is not mechanically derived, or use `not_applicable(...)` only for the schema sentinel `"not_applicable"`.
- `DERIVED_COMPONENTS`: a derived arithmetic/component value needs
  `source_components` for each source-supported operand. Use this for
  calculated values, complements, sums, percentages, denominators, and composed
  values whose parts must be audited.
- `SOURCE_COMPONENT_LITERAL`: each `source_component.value` must appear in its
  own cited component quote or table cell. Fix by citing the component source
  that contains the operand, changing the component value, or removing an
  unsupported component.
- `CELL_ANCHOR`: table-sourced values need a resolvable row/column anchor, and
  the anchored row/cell must contain the claimed value or operand. Fix the row
  label, column label, source table, or source span.
- `QUOTE_LOCATION`: the quote or pattern is not found at the cited location, or
  exists elsewhere. Use exact text from the intended source, change the
  location, or use the SDK quote-location hint to find the right file.
- `NULL_SHAPE`: absent values require explanatory metadata. Use `missing(...)` with a defined reason. Ordinary missingness metadata must not invent a paper/derived value claim. Use `missing(reason=None, explanation=...)` only for an uncovered case; it emits a counted fallback warning. Non-null special values are allowed only when the schema defines them.

- `quote_not_at_location`: the quote exists elsewhere or not at all. Use the
  cited location named by the validator, or replace the quote with exact text
  from the intended source.
- `quote_not_in_bundle`: the quote is not in the corpus text. Re-read the
  bundle source and replace it; do not paraphrase as a quote.
- `value_not_in_quote`: `paper(...)` claims a value that the quote does not
  contain. Follow `PROV_LITERAL`.
- `cell_anchor_missing`, `cell_row_not_found`, `cell_column_not_found`: table
  evidence needs a resolvable row and column anchor. Follow `CELL_ANCHOR`.
- `missing_source_components`: an arithmetic or prose-arithmetic
  `derived(...)` field needs component operands. Follow `DERIVED_COMPONENTS`.
- `source_component_value_not_in_quote`: a component value is not in its own
  component quote. Follow `SOURCE_COMPONENT_LITERAL`.
- `derived_operand_missing_component`: a number in the derivation expression is
  missing from `source_components`. Follow `DERIVED_COMPONENTS`.

### Efficient Validation Queries

Use Python for targeted inspection. Do not read a whole large JSON artifact
unless the compact report is insufficient.

Show grouped validation failures:

```bash
python3 - <<'PY'
import json
d = json.load(open("<project-root>/extractions/.staging/<paper>/validation/<name>.validate.json"))
print(d["failure_count"], d["by_code"])
for g in d["groups"][:10]:
    print(g["code"], g.get("hint_code"), g["field"], g["row_ranges"], g["summary"])
PY
```

Show staged rows by index:

```bash
python3 - <<'PY'
import json
rows = json.load(open("<project-root>/extractions/.staging/<paper>/chunks/<chunk>.rows.json"))["rows"]
for i in [0, 1, 2]:
    print("ROW", i)
    for entry in rows[i]["fields"]:
        if entry["name"] in {"variant_raw", "grade_threshold", "effect_type", "n_events"}:
            print(entry["name"], json.dumps(entry, ensure_ascii=False)[:800])
PY
```

Find the relevant build-script region:

```bash
grep -n "with b.chunk(\"<chunk_name>\")" <project-root>/extractions/.staging/<paper>/scripts/build.py
grep -n "\"<field_name>\"" <project-root>/extractions/.staging/<paper>/scripts/build.py
```

## Repair Or Re-Extraction Scope

When you receive QC repair feedback for a prior extraction:

1. Resume the prior build script.
2. Patch only the named rows/fields for targeted repairs.
3. Preserve unrelated rows and their provenance.
4. Rerun the blessed runner and finalization.
5. Report row counts, changed fields, and validation status.

For a major schema change, still start from the prior build script when
available. Add, remove, or revise fields according to the schema diff, then
rerun validation through the same staged path.

## Success Marker

A successful finalization prints:

```text
[write_extraction iter1] wrote <N> rows to <project-root>/extractions/<paper_id>.jsonl (schema vNNN)
```

Report this marker and the staged validation report path. Do not rerun
finalization just to make the marker appear again.

Missingness explanations apply to extractor-owned scientific fields, not schema-exempt canonical IDs awaiting normalization or optional provenance keys. Keep the existing normalization exemption; do not classify a pending ID as a missing source fact.
