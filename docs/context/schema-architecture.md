# Schema Architecture

## Overview



The schema describes a source-grounded evidence substrate. Schema convergence means that the system can preserve and retrieve the material evidence encountered in the corpus; it does not mean that one downstream synthesis question, inclusion set, arm construction, or model has been chosen correctly.

The substrate must nevertheless provide granular per-paper prerequisite information for four downstream tasks: research-question exploration, inclusion and exclusion, model selection, and analysis execution. Downstream analysis compares papers, infers cross-paper relationships, makes the choices, and executes the model.

## Schema Layers

### Universal Data Primitives
Apply to all scientific papers regardless of domain:
- **Title**
- **Author** / **Lead Author**
- **DOI** (Digital Object Identifier)
- **Publication** (journal name)
- **PMID** (PubMed ID)
- **Study ID**

### Domain-Specific Variables
Defined per topic/channel. For the DPYD pharmacogenetics pilot:
- **Genotype**
- **rsID** (reference SNP identifier)
- **DNA change**
- **Amino acid change**
- **Allele / variant** (specifically "star allele")
- **Individual phenotypic function**
- **Activity score**
- **Malignancies**
- **Grade of outcome severity**
- **Ancestry vs. Race**
- **Therapeutic combinations**
- **CPIC allele described functionalities**
- **Two-by-two outcomes tables**

### Raw Columns vs Calculated Results
- **Raw columns**: Data pulled directly from papers. Expected to be sparse — not every paper reports every variable (the "domain expansion problem").
- **Calculated results**: Universal datasets generated through defined transformations of raw data. These are what meta-analysis tools actually consume.

Calculated or inferred values must remain distinguishable from reported values and retain their source inputs and derivation. A transformation may expose a candidate residual group, compatible integer range, or common effect representation; it must not overwrite the atomic source evidence or silently turn an analytical preference into a reported fact.

### Per-Paper Prerequisite Context and Unknown States

Phase 1 tests whether the discovered schema can accommodate the paper-level facts that downstream users need to compare reports. For duplicate or overlapping cohorts and subgroups, these may include recruitment dates and sites, parent study or dataset identifiers, sample sizes, eligibility criteria, analysis-population and subgroup definitions, and explicit subset, follow-up, extension, or reuse language reported by the paper.

The schema should preserve these facts at their reported scope and granularity, along with explicit ambiguity, missingness, and provenance. It should not encode a cross-paper overlap conclusion as if the source paper reported it.

Phase 1 does not add a relationship graph, cohort linker, issue record, sidecar, or post-normalization surfacing pass. A downstream user or platform agent compares granular paper outputs and reaches cross-paper conclusions. Native cross-paper inference should be reconsidered only if a later downstream-use experiment shows that well-recovered paper-level information is consistently insufficient.

## The Rosetta Stone

A **translation layer** that maps evolving nomenclature across studies from different eras:
- Race → Ancestry (e.g., "white" → "mostly European ancestry")
- Evolving genetic naming conventions over 37 years of DPYD research
- Shifting allele definitions and classification systems

The Rosetta Stone enables "patch-ins" — mapping old terminology onto modern conceptual frameworks without losing the original data.

## Just-in-Time ETL

The system does NOT use a traditional static Extract-Transform-Load pipeline:
- **Extract**: AI-powered extraction from papers into atomic rows
- **Transform**: Dynamic **transformation definitions** run at query time, not at ingestion time
- **Load**: Data stored in raw form; transformations applied on-the-fly

This allows the system to:
- Account for new data dynamically
- Update transformation logic without re-processing stored data
- Support evolving scientific definitions gracefully

## Schema Development Process

Schema definition and extraction are **tightly iterative** — they "blend together":
1. Begin extracting data from papers to discover what variables exist
2. Identify common variables across the paper collection
3. Propose a schema with data types
4. Re-extract to fill gaps and align with the converging schema
5. Handle temporal nomenclature shifts via Rosetta Stone patch-ins

The AI assists in this loop by suggesting data types and identifying which papers contain specific outcomes.

The loop should refine schemas when source evidence is being lost or materially misrepresented. It should not add fields merely because one downstream analysis selected a particular comparator, likelihood, or sensitivity. Domain-specific analytical choices can be expressed in query or transformation definitions while the underlying evidence remains reusable for other questions.

## Evaluation Boundary

Phase 1 schema evaluation should report two separate results:

- **schema accommodation:** whether the discovered schema can faithfully represent each required per-paper prerequisite concept at the right scope and granularity; and
- **prerequisite-fact recovery:** whether extraction recovered and grounded the fact, particularly when the schema could accommodate it.

The evaluation is semantic and schema-agnostic: it judges field meaning, type, scope, and guidance rather than requiring one field name. A separate paper-level fixture and report should sit beside the current field benchmark. Current `field_coverage` output can support semantic recovery adjudication, but it is not a headline composite input for this Phase 1 report.

Cross-paper overlap conclusions and analyst actions may be retained as contextual gold explaining materiality, but they are unscored as Comparanda outputs. A field-value benchmark cannot establish synthesis-decision accuracy, and failure to reproduce one analyst's final inclusion or model choice is not a Phase 1 schema defect.

## Backend Storage



