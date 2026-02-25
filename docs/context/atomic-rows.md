# Atomic Rows

## Definition

An **atomic row** is data abstracted from a scientific study at such a high level of granularity that the information is **no longer divisible** — if divided further, the data loses its meaning.

## Characteristics

- **Maximum granularity**: Each row represents one distinct, indivisible data point.
- **Contextual redundancy**: Rows may contain redundant features from neighboring rows to maintain context (e.g., study ID, author repeated across rows from the same paper).
- **High volume per paper**: A single study can produce hundreds of atomic rows. In the DPYD pilot, one paper generated **302 individual rows**.
- **Completeness goal**: A paper extracted to atomic rows should **never need to be extracted again**.

## Role in the System

### 1. Eliminating Redundant Extraction
The primary purpose — document a paper so thoroughly at the atomic level that no future team needs to re-extract it. This is the "extract once, use forever" principle.

### 2. Auditability and Traceability
Each atomic row is linked back to a **map anchor** on the source PDF. Reviewers can click any data point and see exactly where it was found in the original paper. No more manual hunting with Ctrl+F.


### 4. Inter-Study Translation
Atomic granularity enables the Rosetta Stone translation layer. When different studies use different nomenclature for the same concept, the atomic level provides enough specificity to map between them without conflating definitions.

### 5. Meta-Analysis Input
Atomic rows with proper typing feed directly into meta-analysis tooling. The typed, granular data can be aggregated, filtered, and transformed to produce forest plots, odds ratios, and other statistical outputs.

## "True" Atomic Row

Some team members distinguish between the current focus (extracting from published papers) and the "true" atomic row — the **actual raw data** (individual measurements, participant-level features). Any further division beyond this would require going back to the original study source to collect more data.

## Example: DPYD Pilot

A single pharmacogenetics paper about DPYD variants was decomposed into 302 atomic rows, capturing:
- Each genotype/variant combination
- Associated phenotypic functions and activity scores
- Outcome severity grades
- Therapeutic combinations
- Ancestry/demographic data
- Two-by-two outcome tables
