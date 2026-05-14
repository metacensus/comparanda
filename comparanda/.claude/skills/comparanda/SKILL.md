---
name: comparanda
description: Comparanda extraction pipeline knowledge. Use when working with the DPYD pharmacogenetics extraction system, eval harness, System 2 improvement loop, or any pipeline phase.
---

# Comparanda Pipeline

Agentic system for scientific paper schema extraction. The schema IS the product — the system discovers what structured data to extract, builds normalization tables, and extracts atomic rows for meta-analysis.

## Architecture

- **System 1 (Inner Loop)**: Survey → Schema Proposal → Extraction → Normalization → Validation
- **System 2 (Outer Loop)**: Evaluates System 1 outputs → modifies instruction files → kicks off next iteration
- System 2 modifies the agent instruction files in `.claude/agents/`, never agent code

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

- **Agents**: `.claude/agents/` — System 1 agent prompts (survey, schema-proposer, extractor, normalizer, ma-critique, subset-selector, extraction-qc). System 2 modifies these.
- **Orchestration**: `CLAUDE.md` — phase-by-phase orchestrator instructions for the inner loop.
- **Hooks**: `.claude/hooks/` — validation hooks (e.g. provenance quotes).
- **Scripts**: `scripts/observation_log.py` — observation log query CLI used by the extractor.

## Constraints

- C10: Context window discipline everywhere — multiple focused passes, not everything in one context
- C12: Provenance on everything — paper/derived/external tags
- C13: No data pollution — the domain expert's schema/tables are evaluation-only, System 1 is blind to them
- C14: System 1 agents record reflections (meta-commentary) for System 2
- C15: Gold standard is assumed imperfect — disputed values log
