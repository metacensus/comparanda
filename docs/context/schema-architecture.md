# Schema Architecture

## Overview



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

## Backend Storage



