---
name: comparanda
description: Comparanda extraction pipeline knowledge. Use when working with the Comparanda extraction system, eval harness, System 2 improvement loop, or any pipeline phase.
---

# Comparanda Pipeline

Agentic system for scientific paper schema extraction. The schema IS the product — the system discovers what structured data to extract, builds normalization tables, and extracts atomic rows for meta-analysis.

## Architecture

- **System 1 (Inner Loop)**: Survey → Schema Proposal → Extraction → QC → Normalization → MA Critique
- **System 2 (Outer Loop)**: Evaluates System 1 outputs → modifies instruction files → kicks off next iteration
- System 2 modifies the agent instruction files in `.claude/agents/`, never agent code

## Pipeline Phases

| Phase | Agent | Input | Output |
|-------|-------|-------|--------|
| 0 | (manual / reference generation) | Domain seed | `projects/<domain>/domain/*.md` |
| 0.5 | schema-proposer | Domain context | `schema/survey_schema.json` |
| 1 | survey (per paper) | Paper-prep bundle + survey schema | `surveys/{paper_id}.json` |
| 1.5 | subset-selector | All surveys | Selected representative papers |
| 2 | schema-proposer | Surveys + domain context | `schema/vNNN.yaml` |
| 3a | extractor (per paper) | Bundle + schema + survey | `extractions/{paper_id}.jsonl` |
| 3a-qc | extraction-qc | Extraction + bundle + schema | QC report / repair packet |
| 3b | normalizer | Extractions + schema | `normalization/` tables |
| 3.5 | ma-critique | Schema + extractions + normalization | `critiques/iter_NNN.json` |
| 4 | schema-proposer | MA critique findings | Refined rich schema |

## Key Files

- **Agents**: `.claude/agents/` — System 1 agent prompts (survey, schema-proposer, extractor, normalizer, ma-critique, subset-selector, extraction-qc). System 2 modifies these.
- **Orchestration**: `CLAUDE.md` — phase-by-phase orchestrator instructions for the inner loop.
- **Hooks**: `.claude/hooks/` — extraction write/finalization validation hooks.
- **Scripts**: `scripts/` — extraction SDK, staged write gate, normalization API, and observation-log helpers.

## Constraints

- C10: Context window discipline everywhere — multiple focused passes, not everything in one context
- C12: Provenance on everything — paper/derived/external tags
- C13: No data pollution — gold-standard schemas/tables are evaluation-only, System 1 is blind to them
- C14: System 1 agents record reflections (meta-commentary) for System 2
- C15: Gold standard is assumed imperfect — disputed values log
