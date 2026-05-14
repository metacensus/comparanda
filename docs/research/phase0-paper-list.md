# Phase 0 Paper List — Prior Meta-Analyses & Reference Papers

> Generated 2026-04-02. Papers to acquire for prior-MA extraction and agent instruction development.

## Status Key
- [ ] = Need to acquire
- [x] = Acquired

---

## Section 1: DPYD-Specific Meta-Analyses

These are the papers we'll extract schemas, lessons, and domain traps from to seed agent reference docs.

### 1.1 Le Teuff et al. (2024) — FUSAFE IPD Meta-Analysis (**HIGH PRIORITY**)
- **Title:** Dihydropyrimidine dehydrogenase gene variants for predicting grade 4-5 fluoropyrimidine-induced toxicity: FUSAFE individual patient data meta-analysis
- **Journal:** British Journal of Cancer
- **PMID:** 38225422
- **DOI:** 10.1038/s41416-023-02517-2
- **Open Access:** Yes (PMC10912560)
- **URL:** https://www.nature.com/articles/s41416-023-02517-2
- **Why:** Largest DPYD IPD meta-analysis (25 studies, 8,733 patients). Closest analog to what Comparanda automates. Individual-level genotype/phenotype extraction.
- [x] Acquired

### 1.2 Meulendijks et al. (2015) — Landmark IPD Meta-Analysis (**HIGH PRIORITY**)
- **Title:** Clinical relevance of DPYD variants c.1679T>G, c.1236G>A/HapB3, and c.1601G>A as predictors of severe fluoropyrimidine-associated toxicity
- **Journal:** The Lancet Oncology, 16(16):1639-1650
- **PMID:** 26603945
- **DOI:** 10.1016/S1470-2045(15)00286-7
- **Open Access:** No (Elsevier paywall)
- **URL:** https://pubmed.ncbi.nlm.nih.gov/26603945/
- **Why:** Foundational IPD meta-analysis (8 studies, 7,365 patients). Established adjusted RR for key variants. Essential benchmark.
- [x] Acquired

### 1.3 Sharma et al. (2021) — Treatment-Related Mortality
- **Title:** Pathogenic DPYD Variants and Treatment-Related Mortality in Patients Receiving Fluoropyrimidine Chemotherapy
- **Journal:** The Oncologist, 26(12):1008-1016
- **PMID:** 34506675
- **DOI:** 10.1002/onco.13967
- **Open Access:** Yes (PMC8649021)
- **URL:** https://theoncologist.onlinelibrary.wiley.com/doi/10.1002/onco.13967
- **Why:** Clean study-level extraction schema. 25.6x mortality risk in carriers.
- [x] Acquired

### 1.4 de Moraes et al. (2024) — Mortality + Trial Sequential Analysis
- **Title:** Pharmacogenetics of DPYD and treatment-related mortality on fluoropyrimidine chemotherapy: a meta-analysis and trial sequential analysis
- **Journal:** BMC Cancer
- **PMID:** 39350200
- **DOI:** 10.1186/s12885-024-12981-5
- **Open Access:** Yes
- **URL:** https://link.springer.com/article/10.1186/s12885-024-12981-5
- **Why:** 36 studies, 16,005 patients. Extracts 4 key variants with Mantel-Haenszel method. Good schema reference.
- [x] Acquired

### 1.5 Glewis et al. (2022) — PGx-Guided Dosing vs Standard
- **Title:** A systematic review and meta-analysis of toxicity and treatment outcomes with pharmacogenetic-guided dosing compared to standard of care BSA-based fluoropyrimidine dosing
- **Journal:** British Journal of Cancer
- **PMID:** 35306539
- **DOI:** 10.1038/s41416-022-01779-6
- **Open Access:** Yes (PMC9276780)
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC9276780/
- **Why:** PICO framework, 3 independent reviewers, Excel extraction, Newcastle-Ottawa Scale, GRADE. Good methodology reference.
- [x] Acquired

### 1.6 Kim et al. (2022) — European Population
- **Title:** Elevated Risk of Fluoropyrimidine-Associated Toxicity in European Patients with DPYD Genetic Polymorphism
- **Journal:** Journal of Personalized Medicine, 12(2):225
- **PMID:** 35207713
- **DOI:** 10.3390/jpm12020225
- **Open Access:** Yes (CC BY 4.0)
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC8875904/
- **Why:** Clean extraction schema: author, year, n, country, cancer type, regimen, toxicity definition, genotype frequencies, effect sizes.
- [x] Acquired

### 1.7 Leung et al. (2015) — Asian Population
- **Title:** Association and prediction of severe 5-fluorouracil toxicity with dihydropyrimidine dehydrogenase gene polymorphisms: A meta-analysis
- **Journal:** Biomedical Reports
- **PMID:** 26623034
- **DOI:** 10.3892/br.2015.513
- **Open Access:** Yes (PMC4660586)
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC4660586/
- **Why:** Population-specific analysis, fixed-effects model, Newcastle-Ottawa Scale.
- [x] Acquired

### 1.8 Chan et al. (2024) — Non-European Populations
- **Title:** DPYD genetic polymorphisms in non-European patients with severe fluoropyrimidine-related toxicity: a systematic review
- **Journal:** British Journal of Cancer
- **PMID:** 38886557
- **DOI:** 10.1038/s41416-024-02754-z
- **Open Access:** Yes
- **URL:** https://www.nature.com/articles/s41416-024-02754-z
- **Why:** 32 studies, 53 DPYD variants across 5 ethnic groups. Expands variant landscape beyond "big 4."
- [x] Acquired

### 1.9 Umbrella Review of DPYD Systematic Reviews (2025)
- **Title:** DPYD Genotyping, Fluoropyrimidine Dosage and Toxicity: An Umbrella Review of Systematic Reviews
- **Journal:** Pharmaceuticals, 18(5):727
- **Open Access:** Yes (MDPI)
- **URL:** https://www.mdpi.com/1424-8247/18/5/727
- **Why:** Meta-review of all existing SRs. AMSTAR-2 quality assessment (most rated critically low). Confirms need for better methodology.
- [x] Acquired

### 1.10 Campbell et al. (2016) — Fluoropyrimidine + Platinum Umbrella Review
- **Title:** Fluoropyrimidine and platinum toxicity pharmacogenetics: an umbrella review of systematic reviews and meta-analyses
- **Journal:** Pharmacogenomics
- **PMID:** 26894782
- **DOI:** 10.2217/pgs.15.180
- **Open Access:** No
- **URL:** https://pubmed.ncbi.nlm.nih.gov/26894782/
- **Why:** Broader pharmacogenomic umbrella review. Lower priority than DPYD-specific papers.
- [x] Acquired

---

## Section 2: Meta-Analysis Methodology / Best Practice

These feed universal agent instructions — how to design good meta-analyses, what to extract, quality assessment.

### 2.1 PRISMA 2020 Statement (**HIGH PRIORITY**)
- **Title:** The PRISMA 2020 statement: an updated guideline for reporting systematic reviews
- **Authors:** Page MJ et al.
- **Journal:** BMJ, 372:n71
- **PMID:** 33782057
- **DOI:** 10.1136/bmj.n71
- **Open Access:** Yes (PMC8005924)
- **URL:** https://www.prisma-statement.org/
- **Why:** The reporting standard. 27-item checklist. Essential for MA Critique Agent instructions.
- [x] Acquired

### 2.2 PRISMA 2020 Explanation and Elaboration
- **Title:** PRISMA 2020 explanation and elaboration: updated guidance and exemplars for reporting systematic reviews
- **Authors:** Page MJ et al.
- **Journal:** BMJ, 372:n160
- **Open Access:** Yes (PMC8005925)
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC8005925/
- **Why:** Worked examples for every PRISMA item. Practical reference for agent instructions.
- [x] Acquired

### 2.3 Cochrane Handbook v6.3 (**HIGH PRIORITY**)
- **Title:** Cochrane Handbook for Systematic Reviews of Interventions
- **Authors:** Higgins JPT, Thomas J, et al. (editors)
- **Year:** 2022
- **Open Access:** Yes (free online)
- **URL:** https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current
- **Why:** Chapter 5 on data extraction is the definitive reference. Covers structured forms, dual extraction, quality assessment.
- [x] Acquired (online reference)

### 2.4 CPIC Guideline for DPYD (2017 Update) (**HIGH PRIORITY**)
- **Title:** Clinical Pharmacogenetics Implementation Consortium (CPIC) Guideline for Dihydropyrimidine Dehydrogenase Genotype and Fluoropyrimidine Dosing: 2017 Update
- **Authors:** Amstutz U et al.
- **Journal:** Clinical Pharmacology & Therapeutics
- **DOI:** 10.1002/cpt.911
- **Open Access:** Yes
- **URL:** https://ascpt.onlinelibrary.wiley.com/doi/10.1002/cpt.911
- **Why:** Defines the variant classification schema and activity scoring. The field's ontology. Domain reference for normalization.
- [x] Acquired

---

## Section 3: AI/LLM Systematic Review Automation

These inform our system design — what works, what doesn't, architectural patterns.

### 3.1 Khan et al. (2024) — Dual-LLM Collaborative Extraction (**HIGH PRIORITY**)
- **Title:** Collaborative Large Language Models for Automated Data Extraction in Living Systematic Reviews
- **Journal:** medRxiv (preprint)
- **PMID:** 39399004
- **DOI:** 10.1101/2024.09.20.24314108
- **Open Access:** Yes (CC BY-NC-ND 4.0)
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC11469465/
- **Why:** Closest architectural precedent. Dual-LLM cross-critique pattern (GPT-4-turbo + Claude-3-Opus). 94% accuracy. 23 extracted variables.
- [x] Acquired

### 3.2 Khraisha et al. (2024) — LLMs Replace Humans in SRs?
- **Title:** Can large language models replace humans in systematic reviews?
- **Journal:** Research Synthesis Methods, 15(4):616-626
- **PMID:** 38484744
- **DOI:** 10.1002/jrsm.1715
- **URL:** https://onlinelibrary.wiley.com/doi/10.1002/jrsm.1715
- **Why:** "Human-out-of-the-loop" evaluation. Moderate extraction performance. Informs confidence calibration.
- [x] Acquired

### 3.3 Schmidt et al. (2024) — LLM Extraction Feasibility
- **Title:** Exploring the use of a Large Language Model for data extraction in systematic reviews: a rapid feasibility study
- **Journal:** arXiv (2405.14445)
- **Open Access:** Yes
- **URL:** https://arxiv.org/abs/2405.14445
- **Why:** ~80% accuracy. String variables >80%, numeric harder. BLEU/ROUGE metrics limited; manual assessment preferred.
- [x] Acquired

### 3.4 Guo et al. (2024) — Automated Paper Screening
- **Title:** Automated Paper Screening for Clinical Reviews Using Large Language Models
- **Journal:** JMIR
- **PMID:** 38214966
- **DOI:** 10.2196/48996
- **Open Access:** Yes
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC10818236/
- **Why:** 91% screening accuracy. Python workflow reference.
- [x] Acquired

### 3.5 Li et al. (2025) — GPT-4 for HTA Systematic Reviews
- **Title:** Enhancing systematic literature reviews with generative artificial intelligence
- **Journal:** JAMIA, 32(4):616
- **PMID:** 40036547
- **DOI:** 10.1093/jamia/ocaf030
- **Open Access:** Yes (CC BY-NC)
- **URL:** https://academic.oup.com/jamia/article/32/4/616/8045049
- **Why:** 5-module system with human-in-the-loop. Real-time PICO adjustment. Architecture reference.
- [x] Acquired

### 3.6 Cambridge (2025) — GPT-4o/o3 Data Extraction
- **Title:** Automating the data extraction process for systematic reviews using GPT-4o and o3
- **Journal:** Research Synthesis Methods
- **Open Access:** Check publisher
- **URL:** https://www.cambridge.org/core/journals/research-synthesis-methods/article/automating-the-data-extraction-process-for-systematic-reviews-using-gpt4o-and-o3/28C54A601B80E2BE6611C8B7B15FEDAF
- **Why:** o3 at 75.3%, GPT-4o at 96.3% on external validation. String > numeric accuracy. Open-source system.
- [x] Acquired

### 3.7 Ge et al. (2024) — AI Tools for SRs Survey
- **Title:** Leveraging artificial intelligence to enhance systematic reviews in health research
- **Journal:** Systematic Reviews
- **PMID:** 39456077
- **DOI:** 10.1186/s13643-024-02682-2
- **Open Access:** Yes
- **URL:** https://pmc.ncbi.nlm.nih.gov/articles/PMC11504244/
- **Why:** Comprehensive tool survey. Maps AI tools to SR workflow stages.
- [x] Acquired

### 3.8 JMIR (2026) — Scoping Review of AI Evidence Synthesis Tools
- **Title:** Artificial Intelligence Tools for Automating Evidence Synthesis: Scoping Review
- **Journal:** JMIR, 2026;1:e81597
- **Open Access:** Yes
- **URL:** https://www.jmir.org/2026/1/e81597
- **Why:** Most comprehensive mapping (222 articles, 65 tools). Data extraction covered in 26.1% of studies.
- [x] Acquired

---

## Acquisition Notes

**Paywalled (need institutional access or purchase):**
- Meulendijks 2015 (Lancet Oncology) — HIGH PRIORITY
- Campbell 2016 (Pharmacogenomics) — lower priority
- Khraisha 2024 (Research Synthesis Methods) — check

**Everything else is open access.**

**Recommended acquisition order:**
1. Le Teuff 2024, Meulendijks 2015, CPIC 2017, PRISMA 2020 (core references)
2. Khan 2024, de Moraes 2024, Kim 2022 (schema extraction targets)
3. Cochrane Handbook Ch5, PRISMA E&E (methodology deep dives)
4. Remaining papers as needed
