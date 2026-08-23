# Schema Architecture

## Overview



The schema describes a source-grounded evidence substrate. Schema convergence means that the system can preserve and retrieve the material evidence encountered in the corpus; it does not mean that one downstream synthesis question, inclusion set, arm construction, or model has been chosen correctly.

The substrate must nevertheless be sufficient to inform exploration and selection of research questions, inclusion/exclusion review, statistical-model selection, and construction of the inputs needed to run the selected model. Comparanda represents the evidence and constraints for those tasks; downstream analysis makes the choices and executes the model.

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

### Evidence Relationships and Unknown States

Atomic rows are necessary but not always sufficient. Downstream usability can depend on relationships that span rows or papers, including:

- `same_as`, `subset_of`, `possibly_overlaps`, and `disjoint_from`;
- shared participants, controls, datasets, measures, or observation waves;
- marginal groups whose joint distribution is unknown;
- variable, asymmetric, or indeterminate observation windows; and
- treatment or exposure changes occurring inside an outcome window.

These relationships may initially be represented as traceable views or sidecar issue records rather than new columns on every atomic row. The physical storage design should be driven by repeated cross-domain need. Unknown and non-identifiable states are valid evidence outputs and must not be coerced into precise values for schema completeness.

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

Schema evaluation should report separate layers:

- field and claim recovery/faithfulness;
- relationship, timing, and uncertainty representation; and
- issue surfacing and sufficiency for downstream review.

A field-value benchmark cannot establish synthesis-decision accuracy. Conversely, failure to reproduce one analyst's final inclusion or model choice is not a schema defect when the source evidence, material relationships, and uncertainty were preserved and surfaced adequately.

## Backend Storage



