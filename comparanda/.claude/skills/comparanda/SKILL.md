---
name: comparanda
description: Comparanda extraction pipeline knowledge. Use when working with the DPYD pharmacogenetics extraction system, eval harness, System 2 improvement loop, or any pipeline phase.
---

# Comparanda Pipeline

Agentic system for scientific paper schema extraction. The schema IS the product — the system discovers what structured data to extract, builds normalization tables, and extracts atomic rows for meta-analysis.

## Architecture

- **System 1 (Inner Loop)**: Survey → Schema Proposal → Extraction → Normalization → Validation
- **System 2 (Outer Loop)**: Evaluates System 1 outputs → modifies instruction files → kicks off next iteration
- System 2 modifies instruction files in `src/comparanda/instructions/`, never agent code

## Pipeline Phases

| Phase | Agent | Input | Output |
|-------|-------|-------|--------|
| 0 | (manual) | Domain seed | `data/domain_context/*.md` |
| 0.5 | survey-schema-generator | Domain context | `survey_schema.json` |
| 1 | survey (per paper) | Paper + survey schema | `surveys/{pmid}.json` |
| 1.5 | (subset selector) | All surveys | Selected 5-8 papers |
| 2 | schema-proposer | All surveys + domain context | `schema/vNNN.json` |
| 3 | extractor (per paper) | Paper + schema | `extractions/{pmid}.jsonl` |
| 3.5 | normalizer | Extractions | Updated extractions + `normalization/` tables |
| 4 | (eval harness) | Extractions vs gold standard | Scores + report |
| 5 | (progressive expansion) | Add papers, re-extract | Overfitting detection |

## Key Files

- **Eval harness**: `src/comparanda/eval/` — matchers, alignment, scoring, LLM judge, regression detection
- **System 2 loop**: `src/comparanda/loop/` — lessons, trajectory, playbook, runner
- **Instruction files**: `src/comparanda/instructions/` — System 2 modification targets
- **Gold standard**: `data/raw/spreadsheets/gold_standard.xlsx` (gitignored)

## Constraints

- C10: Context window discipline everywhere — multiple focused passes, not everything in one context
- C12: Provenance on everything — paper/derived/external tags
- C13: No data pollution — the domain expert's schema/tables are evaluation-only, System 1 is blind to them
- C14: System 1 agents record reflections (meta-commentary) for System 2
- C15: Gold standard is assumed imperfect — disputed values log

## Running Tests

```bash
uv run pytest tests/
```
