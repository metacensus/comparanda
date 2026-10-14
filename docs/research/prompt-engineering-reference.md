# Prompt Engineering Reference for Claude Agents

A comprehensive reference for writing high-quality prompts for Claude (Anthropic) models,
focused on agentic systems, scientific data extraction, structured output, and evaluation.

Last updated: 2026-04-02

---

## Table of Contents

1. [Core Principles](#1-core-principles)
2. [Model Selection Guide](#2-model-selection-guide)
3. [Agentic System Prompts](#3-agentic-system-prompts)
4. [Scientific / Domain Expert Prompts](#4-scientific--domain-expert-prompts)
5. [Structured Extraction Prompts](#5-structured-extraction-prompts)
6. [Critique / Evaluation Prompts](#6-critique--evaluation-prompts)
7. [Few-Shot vs Zero-Shot Strategies](#7-few-shot-vs-zero-shot-strategies)
8. [Prompt Architecture for Multi-Agent Systems](#8-prompt-architecture-for-multi-agent-systems)
9. [Anti-Patterns](#9-anti-patterns)
10. [Template Library](#10-template-library)
11. [Sources](#11-sources)

---

## 1. Core Principles

These principles are drawn from Anthropic's official prompting best practices for Claude 4.x
models (Opus 4.6, Sonnet 4.6, Haiku 4.5).

### 1.1 Be Clear and Direct

Claude responds best to explicit instructions. Do not rely on the model to infer intent from
vague prompts.

**Colleague test** (adapted from Anthropic's guidance): imagine handing the prompt to a
capable coworker who knows nothing about the task. Anything that would leave them guessing
will leave the model guessing too.

- Be specific about the desired output format and constraints.
- Provide instructions as sequential steps using numbered lists or bullet points when order
  matters.
- If you want "above and beyond" behavior, explicitly request it.

### 1.2 Provide Context and Motivation

Explain *why* an instruction matters. Claude generalizes from explanations better than from
bare rules.

```
# Less effective
Never abbreviate units.

# More effective
These values will be parsed by a script that only recognizes full unit names,
so always write units out in full (e.g. "milligrams", not "mg").
```

### 1.3 Use XML Tags for Structure

XML tags help Claude parse complex prompts unambiguously. They separate instructions from
context, examples, and variable inputs.

- Use consistent, descriptive tag names (`<instructions>`, `<context>`, `<input>`,
  `<examples>`, `<document>`).
- Nest tags for hierarchical content.
- Wrap each document in `<document index="n">` with `<source>` and `<document_content>`
  subtags.

### 1.4 Give Claude a Role

A role statement in the system prompt focuses behavior and tone. Even one sentence helps:

```
You are a pharmacogenetics expert specializing in drug-gene interaction
classification for systematic reviews.
```

### 1.5 Long Context: Documents First, Query Last

For inputs exceeding 20k tokens, place longform data at the top and your query/instructions
at the bottom. Anthropic reports this improves response quality by up to 30%.

### 1.6 Ground Responses in Quotes

For long-document tasks, instruct Claude to quote relevant passages before performing its
task. This forces attention to source material and reduces hallucination.

```
Find quotes from the source documents relevant to answering the extraction
question. Place these in <quotes> tags. Then, based on these quotes, produce
your extraction. Place results in <extraction> tags.
```

### 1.7 Tell Claude What To Do, Not What Not To Do

Positive framing is more effective than prohibitions.

```
# Less effective
Don't leave any field blank.

# More effective
Fill every field; when the paper does not report a value, record it as
missing with a short explanation of why.
```

### 1.8 Prefer General Instructions Over Prescriptive Steps

Anthropic's extended-thinking guidance observes that a high-level nudge to reason carefully
tends to work better than a detailed, hand-authored reasoning procedure, because the model's
own plan is often better than the one a prompt author would write. Guide the *what* and
*why*; let the model decide *how*.

---

## 2. Model Selection Guide

Based on Anthropic's official guidance (as of 2026-04):

| Model | Best For | Effort Setting | Notes |
|---|---|---|---|
| **Opus 4.6** | Hardest, longest-horizon problems: large-scale code migrations, deep research, extended autonomous work, complex multi-step extraction | `high` or `max` | Significantly more upfront exploration; may overthink on simple tasks |
| **Sonnet 4.6** | Workhorse for most applications: agentic coding, data extraction, tool-heavy workflows | `medium` (default `high`) | Best price/performance for most agent tasks; set effort to `medium` for latency-sensitive work |
| **Haiku 4.5** | High-volume, low-latency: classification, routing, simple extraction, subagent workers | `low` | Fastest; use for parallelized subagent tasks where speed matters more than depth |

### Adaptive Thinking

Claude 4.6 models use adaptive thinking (`thinking: {type: "adaptive"}`), where the model
dynamically decides when and how much to think. Control via the `effort` parameter:

- `max` -- deepest reasoning, highest token usage
- `high` -- strong reasoning, good for agentic loops
- `medium` -- balanced; recommended starting point for most tasks
- `low` -- minimal thinking, fastest responses

### Cost Optimization Pattern

Use model routing: Haiku for initial classification/routing, Sonnet for extraction and
synthesis, Opus for complex judgment calls and final evaluation. This mirrors Anthropic's
own multi-agent research system (Opus as lead, Sonnet as subagents).

---

## 3. Agentic System Prompts

### 3.1 Architectural Patterns

From Anthropic's "Building Effective Agents" guide, five workflow patterns exist. Choose
based on task structure:

| Pattern | When to Use |
|---|---|
| **Prompt Chaining** | Fixed sequence of subtasks; you need to inspect intermediate results |
| **Routing** | Inputs fall into distinct categories needing specialized handling |
| **Parallelization** | Independent subtasks can run simultaneously |
| **Orchestrator-Workers** | Subtasks are dynamic and determined at runtime |
| **Evaluator-Optimizer** | Clear evaluation criteria; iterative refinement helps |

**Key principle**: Start with the simplest pattern that works. Add complexity only when it
demonstrably improves outcomes.

### 3.2 System Prompt Structure for Agents

Organize into distinct sections using XML tags or markdown headers. Anthropic's context
engineering guidance calls this the "right altitude" -- balance specificity with flexibility.

```xml
<role>
You are [role description with domain expertise].
</role>

<background_information>
[Domain context the agent needs to understand]
</background_information>

<instructions>
[Core behavioral instructions -- what to do, in what order, with what constraints]
</instructions>

<tool_guidance>
[When and how to use each available tool]
</tool_guidance>

<output_format>
[Exact specification of expected output structure]
</output_format>

<examples>
[2-5 representative examples covering normal cases and edge cases]
</examples>

<guardrails>
[Safety constraints, confirmation requirements, error handling]
</guardrails>
```

### 3.3 Autonomy and Safety

Claude Opus 4.6 may take irreversible actions without guidance. Add explicit guardrails:

```
Before acting, weigh how reversible the action is and what it could affect.
Go ahead with local, easily undone work such as editing files in your scope
or running tests. Pause and ask before anything destructive, hard to undo,
or touching shared resources.
```

### 3.4 Tool Documentation

Anthropic recommends investing as much care in tool definitions (names, descriptions,
parameters) as in the prompt itself. Each tool needs:

- A distinct purpose and clear description
- Descriptive parameter names with docstring-quality descriptions
- Example usage, edge cases, input requirements, and tool boundaries
- Poka-yoke design (modify arguments to prevent mistakes)

### 3.5 Parallel Tool Calling

Claude 4.6 excels at parallel execution. Boost to near-100% with:

```xml
<parallel_tools>
When several tool calls don't depend on each other, issue them together in
one step. When a call needs a value produced by an earlier call, wait for
that result first. Don't invent placeholder or guessed arguments.
</parallel_tools>
```

### 3.6 Long-Horizon State Tracking

For agents that span multiple context windows:

1. Use the first context window to set up framework (tests, scripts, TODO list).
2. Track state in structured files (`progress.json`, `tests.json`).
3. Use git for checkpoint/restore across sessions.
4. Write progress notes in unstructured text for general context.
5. On context refresh, start by reviewing state files rather than relying on compaction.

```
Older context will be summarized automatically as you near the limit, so
keep working; don't wrap up early to save tokens. When you get close to the
limit, write your progress and current state to the state files so you can
pick up cleanly after the refresh.
```

### 3.7 Controlling Overeagerness (Claude 4.5/4.6 Specific)

These models are more proactive than predecessors. If prompts were designed to reduce
undertriggering (e.g., "CRITICAL: You MUST use this tool"), dial back to normal language.

```
Keep the work to what was asked or is plainly required. Prefer the simplest
solution that does the job; don't add extras.
```

---

## 4. Scientific / Domain Expert Prompts

### 4.1 Role Anchoring for Domain Expertise

Set a specific expert role with credentials-level framing:

```
You are a senior pharmacogenetics researcher with expertise in:
- Drug-gene interaction classification (CPIC guidelines)
- Meta-analysis methodology (Cochrane standards)
- Biomedical ontologies (PharmGKB, ClinVar, MeSH)

Your task is to extract structured data from clinical trial publications
with the rigor expected for a systematic review.
```

### 4.2 Decomposed Extraction Prompting

Research on LLM-based systematic review data extraction (Gartlehner et al., 2025; PMC
collaborative LLM study) shows that decomposed zero-shot prompting -- breaking extraction
into variable-by-variable tasks -- outperforms monolithic extraction.

**Strategy**: Extract one variable or variable group at a time rather than all at once.

```xml
<extraction_task>
From the following clinical trial report, extract ONLY the following
variable:

Variable: Primary efficacy outcome (continuous)
Definition: The main efficacy endpoint as reported in the study results,
  including the metric name, value, unit, confidence interval, and p-value.

If the variable is not reported, respond with {"status": "not_reported"}.
If the variable is ambiguous, extract all plausible values and flag with
{"status": "ambiguous", "candidates": [...]}.
</extraction_task>

<document>
{{STUDY_TEXT}}
</document>
```

### 4.3 Two-Reviewer LLM Workflow

The collaborative LLM approach for systematic reviews (PMC11469465) simulates the
traditional two-reviewer process:

1. Two independent LLM extractions (can be same model, different prompts/temperatures)
2. Concordant responses accepted as reliable (accuracy ~0.94 in testing)
3. Discordant responses cross-critiqued
4. Cross-critique improved accuracy from 0.41-0.50 to 0.76

**Implementation**: Run extraction twice, compare outputs programmatically, send disagreements
to a critique/resolution prompt.

### 4.4 Grounding in Source Text

Always require the model to cite specific passages before producing extractions:

```
Before extracting data, identify the specific sentences or passages in the
source document that contain the relevant information. Quote these passages
verbatim in <evidence> tags. Then produce your extraction based solely on
the quoted evidence. If no evidence exists for a field, mark it as null
rather than inferring.
```

### 4.5 Biomedical Information Extraction Pipeline

From LLM-IE (PMC11901043), effective biomedical extraction prompts have four components:

1. **Task description** -- Clear explanation of the extraction objective
2. **Schema definition** -- Structured specification of entities and attributes
3. **Output format definition** -- JSON schema for expected response structure
4. **Input placeholders** -- Variables for target document text

Three extraction strategies by accuracy/cost tradeoff:

| Strategy | Description | Best For |
|---|---|---|
| **BasicFrame** | Direct single-pass extraction | Simple entities, low cost |
| **ReviewFrame** | Two-stage: extract then amend/correct | Higher accuracy needed |
| **SentenceFrame** | Segment into sentences, extract per-sentence | Best recall, highest cost |

### 4.6 Handling Missing and Ambiguous Data

Scientific extraction must distinguish between "not reported," "reported as zero/null," and
"ambiguous." Build this into the schema:

```json
{
  "field_name": "sample_size",
  "value": null,
  "status": "not_reported | reported | ambiguous | derived",
  "evidence_quote": "string or null",
  "confidence": "high | medium | low",
  "notes": "optional extraction notes"
}
```

---

## 5. Structured Extraction Prompts

### 5.1 Structured Outputs (Constrained Decoding)

As of late 2025, Claude supports **structured outputs** via `output_config.format`, which
constrains token generation at inference time to guarantee valid JSON matching your schema.
This is fundamentally different from prompting the model to return JSON.

Two complementary features:

- **JSON outputs** (`output_config.format`): Controls Claude's response format. The response
  lands in `response.content[0].text` as guaranteed-valid JSON.
- **Strict tool use** (`strict: true`): Guarantees schema validation on tool names and inputs.

```python
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    messages=[{"role": "user", "content": extraction_prompt}],
    output_config={
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string"},
                    "sample_size": {"type": ["integer", "null"]},
                    "effect_size": {"type": ["number", "null"]},
                    "confidence_interval": {
                        "type": ["object", "null"],
                        "properties": {
                            "lower": {"type": "number"},
                            "upper": {"type": "number"}
                        }
                    }
                },
                "required": ["study_id", "sample_size", "effect_size"],
                "additionalProperties": False
            }
        }
    }
)
```

### 5.2 Schema Design Best Practices

From Anthropic's structured outputs documentation:

- **Keep schemas focused**: Deeply nested schemas with many required fields are harder to
  satisfy. Start simple and add complexity as needed.
- **Use `additionalProperties: false`** for strict validation.
- **Write descriptive field names**: Claude interprets names semantically.
- **Match schema to task**: If the task might not have all information your schema requires,
  make those fields optional (nullable).
- **Use clear prompts**: Ambiguous prompts make it harder for the model to know what output
  to produce.

### 5.3 Limitations

- Structured outputs are **incompatible with citations** (returns 400 error).
- Structured outputs are **incompatible with message prefilling** in JSON outputs mode.
- Prefilled responses on the last assistant turn are deprecated in Claude 4.6.

### 5.4 When to Use Structured Outputs vs Tool Use vs Prompt-Only

| Approach | Use When |
|---|---|
| **Structured outputs** (`output_config.format`) | Data extraction, report generation, any task where the *final response* must be valid JSON |
| **Strict tool use** (`strict: true`) | Agent tool calls that must validate against a schema; when the model needs to decide *which* tool to call |
| **Prompt-only JSON** | Quick prototyping; when you need JSON within a larger text response; when combined with citations |
| **XML tags in prompt** | When you need structured output *within* a conversational response; when you want both reasoning and structured data |

### 5.5 JSONL Streaming Pattern

For batch extraction across many documents, use JSONL (one JSON object per line):

```
Extract the following fields from each study. Output one JSON object per
study, one per line (JSONL format). Do not include any text outside the
JSON objects.

Fields: study_id, first_author, year, sample_size, intervention,
comparator, primary_outcome, effect_size, ci_lower, ci_upper, p_value

<studies>
{{STUDY_TEXTS}}
</studies>
```

---

## 6. Critique / Evaluation Prompts

### 6.1 LLM-as-Judge Design Principles

From Anthropic's multi-agent research system and LLM-as-judge literature:

- **Single criterion per evaluation**: Each evaluation monitors one dimension. Combining
  relevance + accuracy + completeness in one prompt degrades all three.
- **Rubric with concrete anchors**: Define what each score level means with examples.
- **Chain-of-thought before scoring**: Require the evaluator to reason before assigning a
  score. This surfaces hallucinations and improves consistency.
- **Structured output for scores**: Use JSON to capture score + reasoning + evidence.

### 6.2 Rubric Template

```xml
<evaluation_task>
You are evaluating the quality of a data extraction from a scientific paper.

<rubric>
Score each dimension on a 1-5 scale:

COMPLETENESS (were all requested fields extracted?)
  5: All fields extracted with values or explicitly marked as not reported
  4: 1-2 minor fields missing without explanation
  3: Several fields missing; most key fields present
  2: Many fields missing; extraction is fragmentary
  1: Extraction is mostly empty or placeholder values

ACCURACY (do extracted values match the source document?)
  5: All values exactly match source text; evidence quotes provided
  4: Minor discrepancies (e.g., rounding) that do not change interpretation
  3: 1-2 substantive errors in extracted values
  2: Multiple errors; some values appear fabricated
  1: Extraction bears little relation to source document

EVIDENCE GROUNDING (are extractions supported by cited passages?)
  5: Every extracted value has a verbatim quote from the source
  4: Most values have supporting quotes; 1-2 lack evidence
  3: Some values supported; others lack evidence
  2: Few values have supporting evidence
  1: No evidence provided; values appear unsupported
</rubric>

<extraction_to_evaluate>
{{EXTRACTION}}
</extraction_to_evaluate>

<source_document>
{{DOCUMENT}}
</source_document>

First, analyze the extraction against the source document. Identify specific
matches and mismatches. Then score each dimension with justification.
</evaluation_task>
```

### 6.3 Self-Correction Pattern

The most common chaining pattern from Anthropic: generate a draft, evaluate against
criteria, refine based on the evaluation.

```
Step 1 (Generator): Extract data from the document.
Step 2 (Evaluator): Score the extraction against the rubric.
Step 3 (Refiner):   Given the extraction and evaluation, fix identified issues.
```

Each step is a separate API call so you can log, branch, or retry at any point.

### 6.4 Bias Mitigation

From LLM-as-judge research:

- **Randomize order**: If comparing two outputs, randomize which appears first (position
  bias is well-documented).
- **Avoid revealing provenance**: Do not tell the judge which model/method produced each
  output.
- **Use specific rubrics**: Vague criteria like "quality" lead to inconsistent scoring.
  Always decompose into concrete, measurable dimensions.
- **Calibrate with known examples**: Test the judge on examples where you know the correct
  score. Adjust the rubric until the judge aligns with human judgment.
- **Domain-specific gaps**: Expert knowledge tasks show LLM-human agreement rates of only
  64-68%, well below inter-expert baselines. Plan for human review on domain-critical
  judgments.

### 6.5 Gap Identification Prompt

For agents that identify what is missing rather than scoring what is present:

```xml
<gap_analysis_task>
You are reviewing an extraction for completeness against a predefined schema.

<required_schema>
{{SCHEMA_DEFINITION}}
</required_schema>

<current_extraction>
{{EXTRACTION}}
</current_extraction>

<source_document>
{{DOCUMENT}}
</source_document>

Identify:
1. Fields present in the schema but missing from the extraction
2. Fields extracted but with suspiciously generic or placeholder values
3. Fields where the source document contains information not captured
4. Inconsistencies between extracted values (e.g., sample size vs subgroup totals)

For each gap, provide:
- The field name
- What the source document says (with quote)
- What action should be taken (extract, re-extract, flag for human review)
</gap_analysis_task>
```

---

## 7. Few-Shot vs Zero-Shot Strategies

### 7.1 Decision Framework

| Situation | Recommendation |
|---|---|
| Well-defined task, Claude understands the domain | Zero-shot first; add examples only if output quality is insufficient |
| Specific output format/style required | Few-shot (3-5 examples) |
| Edge cases are common or ambiguous | Few-shot with edge case examples |
| High-volume extraction with consistent schema | Few-shot (examples act as specification) |
| Novel or unusual task structure | Few-shot; examples worth more than instructions |
| Cost/latency constrained (e.g., Haiku subagent) | Zero-shot with clear instructions; examples add tokens |

### 7.2 How Many Examples

From Anthropic's guidance: **3-5 examples for best results.** Research suggests:

- 1 example: Establishes format and basic expectation
- 3 examples: Covers normal variation and begins to define boundaries
- 5 examples: Covers edge cases and disambiguates tricky patterns
- 8+ examples: Diminishing returns; may reduce flexibility. LLM-as-judge research shows
  that *more examples can reduce effectiveness* for evaluation tasks.

For biomedical extraction specifically, the LLM-IE benchmarks showed competitive performance
with only 8-shot examples in resource-limited contexts.

### 7.3 Example Formatting

Wrap examples in dedicated tags so Claude distinguishes them from instructions:

```xml
<examples>
  <example index="1">
    <input>
      Patient was prescribed warfarin 5mg daily. CYP2C9 *1/*3 genotype.
    </input>
    <output>
      {
        "drug": "warfarin",
        "dose": "5mg daily",
        "gene": "CYP2C9",
        "diplotype": "*1/*3",
        "phenotype": "intermediate_metabolizer",
        "recommendation": "Consider reduced dose per CPIC guidelines"
      }
    </output>
  </example>
  <example index="2">
    <input>
      No pharmacogenomic testing was performed prior to prescribing clopidogrel.
    </input>
    <output>
      {
        "drug": "clopidogrel",
        "dose": null,
        "gene": null,
        "diplotype": null,
        "phenotype": null,
        "recommendation": null,
        "notes": "PGx testing not performed; no genotype data available"
      }
    </output>
  </example>
</examples>
```

### 7.4 Example Design Principles

- **Diverse**: Cover different scenarios, not variations of the same one.
- **Include edge cases**: Missing data, ambiguous inputs, null values.
- **Show reasoning if useful**: Use `<thinking>` tags in examples to demonstrate the
  reasoning pattern you want.
- **Avoid unintended patterns**: If all examples have the same structure, Claude may
  over-fit to that structure. Vary length, complexity, and field values.

### 7.5 Thinking in Examples

Anthropic notes that few-shot examples combine well with extended thinking: if the
examples include their reasoning inside `<thinking>` tags, the model tends to carry that
reasoning style into its own thinking.

```xml
<example>
  <input>Study reports OR = 1.5 (95% CI: 0.9-2.3, p = 0.08)</input>
  <thinking>
  The odds ratio is 1.5 with a confidence interval that crosses 1.0
  (0.9-2.3), and the p-value is 0.08 which exceeds the conventional
  0.05 threshold. This result is not statistically significant.
  </thinking>
  <output>
  {
    "effect_measure": "OR",
    "effect_size": 1.5,
    "ci_lower": 0.9,
    "ci_upper": 2.3,
    "p_value": 0.08,
    "statistically_significant": false
  }
  </output>
</example>
```

---

## 8. Prompt Architecture for Multi-Agent Systems

### 8.1 Anthropic's Recommended Pattern: Orchestrator-Workers

From Anthropic's own multi-agent research system, which outperformed single-agent Claude
Opus 4 by 90.2%:

- **Lead agent** (Opus): Analyzes queries, develops strategy, delegates to subagents,
  synthesizes results
- **Subagents** (Sonnet): Execute focused tasks with independent context windows
- **Citation agent**: Post-processes for source verification

### 8.2 Prompt Layering Architecture

For systems where agents share universal instructions plus domain-specific context:

```
Layer 1: UNIVERSAL SYSTEM PROMPT (shared across all agents)
  - Organization identity and values
  - Output quality standards
  - Safety and ethical constraints
  - Common output formatting rules

Layer 2: ROLE-SPECIFIC SYSTEM PROMPT (per agent type)
  - Domain expertise and role definition
  - Task-specific instructions
  - Tool guidance for this agent's tools
  - Success criteria for this agent's tasks

Layer 3: TASK CONTEXT (injected per invocation)
  - Specific documents/data to process
  - Task parameters and constraints
  - Examples relevant to this specific task
```

**Implementation**: Concatenate layers into the system prompt. Universal instructions first,
then role-specific, then task context. This mirrors how Claude Code's own system prompt
works (base instructions + project-specific CLAUDE.md + per-task context).

### 8.3 Delegation Prompt Design

When the lead agent delegates to subagents, the delegation prompt must include:

1. **Clear objective**: What the subagent needs to accomplish
2. **Output format**: Exact structure of the expected response
3. **Tool/source guidance**: Which tools and sources to use
4. **Task boundaries**: What is in-scope and out-of-scope
5. **Effort budget**: How many tool calls / how much depth is appropriate

Anthropic's multi-agent research write-up reports that underspecified delegation led
subagents to repeat each other's work and search in the wrong directions.

### 8.4 Effort Scaling Rules

From Anthropic's multi-agent research system, embed effort guidelines to prevent
overinvestment in simple queries:

| Task Complexity | Subagents | Tool Calls | Example |
|---|---|---|---|
| Simple fact-finding | 1 | 3-10 | "What is the half-life of warfarin?" |
| Comparison/analysis | 2-4 | 10-15 | "Compare efficacy of drug A vs drug B across 5 studies" |
| Complex research | 5-10+ | 15+ | "Synthesize all evidence on CYP2D6 and codeine metabolism" |

### 8.5 Inter-Agent Communication

- **Subagents output to files**, not conversation history. This prevents the "game of
  telephone" effect where information degrades through conversation turns.
- **Lightweight references**: Subagents return references (file paths, summaries) to the
  coordinator rather than full outputs.
- **Separate context windows**: Each subagent gets a clean context, preventing cross-
  contamination of reasoning.
- **Structured handoff**: Use JSON for inter-agent communication to enable programmatic
  validation.

### 8.6 Context Window Management

Token efficiency explained 80% of performance variance in Anthropic's evaluations.

- Multi-agent systems use approximately 15x more tokens than single-model chats.
- Compaction: Summarize conversation contents nearing the limit. Preserve decisions and
  unresolved issues; discard redundant outputs.
- Sub-agents should return condensed summaries (1,000-2,000 tokens), not full exploration
  logs.
- Use tool result clearing as the lightest-touch form of compaction.

---

## 9. Anti-Patterns

### 9.1 Prompt Anti-Patterns

| Anti-Pattern | Why It Fails | Fix |
|---|---|---|
| **Vague instructions** ("analyze this data") | Claude cannot infer your specific requirements | Specify exact fields, format, and success criteria |
| **Negative-only constraints** ("don't hallucinate") | Does not tell the model what to do instead | "Extract only values explicitly stated in the source. For values not found, return null." |
| **Over-prompting for thoroughness** (Claude 4.6) | Newer models are already proactive; aggressive prompting causes overtriggering | Use normal language: "Use this tool when..." not "CRITICAL: You MUST use this tool" |
| **Monolithic extraction** (all fields at once) | Error rate increases with extraction complexity | Decompose into variable groups; extract iteratively |
| **No examples for format-sensitive tasks** | Claude guesses at format; inconsistent output | Provide 3-5 diverse examples |
| **Examples without edge cases** | Model overfits to happy-path pattern | Include examples with missing data, ambiguity, null values |
| **Single-pass evaluation** | One model can hallucinate or miss errors | Use two-pass: extract then evaluate, or two independent extractions |
| **Combining evaluation criteria** | Muddies scoring; reduces reliability on each dimension | One criterion per evaluation prompt |
| **Hardcoded logic in system prompts** | Brittle; fragments maintenance | Balance specificity with flexibility (the "right altitude" principle) |
| **Prefilled assistant responses** | Deprecated in Claude 4.6 | Use structured outputs, XML tags, or explicit format instructions |
| **Generic role descriptions** | "You are a helpful assistant" adds no value | Anchor with specific domain expertise and credential-level framing |

### 9.2 Structural Anti-Patterns

| Anti-Pattern | Why It Fails | Fix |
|---|---|---|
| **Agent when workflow suffices** | Higher cost, latency, and debugging difficulty | Use agents only for open-ended problems with unpredictable steps |
| **No inter-agent schema** | Agents produce incompatible outputs | Define shared JSON schemas for all handoff points |
| **Full context forwarding** | Sub-agents inherit irrelevant context; token waste | Pass only task-specific context; use file-based handoff |
| **No effort budgeting** | Simple queries get expensive deep research | Embed effort scaling rules per task complexity |
| **No verification loop** | Errors compound across agent steps | Add evaluation/critique step after each substantive extraction |

---

## 10. Template Library

### 10.1 Base System Prompt Template (Universal Layer)

```xml
<identity>
You are an agent in the Comparanda system, a tool for automating
pharmacogenetic meta-analysis and systematic review data extraction.
</identity>

<standards>
- Extract only information explicitly stated in source documents.
- When information is absent, return null rather than inferring.
- Provide evidence quotes for every extracted value.
- Use the exact field names and types defined in the output schema.
- Flag ambiguity rather than silently resolving it.
</standards>

<safety>
- Never fabricate citations or data values.
- When uncertain, express uncertainty with a confidence field rather
  than omitting the uncertainty.
- Preserve original units and terminology from source documents.
</safety>
```

### 10.2 Extraction Agent Template

```xml
<role>
You are a data extraction specialist for systematic reviews. You extract
structured data from clinical trial publications following Cochrane and
PRISMA standards.
</role>

<task>
Extract the following variables from the provided study document.
For each variable, provide:
1. The extracted value (in the specified type)
2. A verbatim quote from the source supporting the extraction
3. A confidence level (high/medium/low)
4. Status (reported/not_reported/ambiguous/derived)
</task>

<variables>
{{VARIABLE_DEFINITIONS}}
</variables>

<output_schema>
{{JSON_SCHEMA}}
</output_schema>

<examples>
{{FEW_SHOT_EXAMPLES}}
</examples>

<document>
{{STUDY_TEXT}}
</document>

Extract the specified variables from the document above. Ground every
extraction in a direct quote. If a variable is not reported in the
document, mark its status as "not_reported" and set the value to null.
```

### 10.3 Critique Agent Template

```xml
<role>
You are a quality assurance reviewer for systematic review data extractions.
You verify extractions against source documents, identifying errors, gaps,
and ambiguities.
</role>

<task>
Review the following extraction against the source document. Evaluate on
three dimensions: completeness, accuracy, and evidence grounding. For each
issue found, specify the field, the problem, and the correction needed.
</task>

<rubric>
{{EVALUATION_RUBRIC}}
</rubric>

<extraction>
{{EXTRACTION_TO_REVIEW}}
</extraction>

<source_document>
{{ORIGINAL_DOCUMENT}}
</source_document>

<output_format>
{
  "scores": {
    "completeness": {"score": 1-5, "justification": "..."},
    "accuracy": {"score": 1-5, "justification": "..."},
    "evidence_grounding": {"score": 1-5, "justification": "..."}
  },
  "issues": [
    {
      "field": "field_name",
      "issue_type": "missing | incorrect | unsupported | ambiguous",
      "current_value": "what the extraction says",
      "correct_value": "what the source says (with quote)",
      "severity": "critical | major | minor"
    }
  ],
  "overall_assessment": "accept | revise | reject",
  "revision_instructions": "specific instructions for fixing issues"
}
</output_format>
```

### 10.4 Orchestrator Agent Template

```xml
<role>
You are the lead researcher coordinating a systematic review data
extraction pipeline. You decompose complex extraction tasks, delegate
to specialized subagents, and synthesize their results.
</role>

<available_agents>
{{AGENT_DESCRIPTIONS_AND_CAPABILITIES}}
</available_agents>

<effort_guidelines>
- Simple lookups (1-3 variables from a clear source): handle directly
- Standard extraction (full study profile): delegate to extraction agent
- Complex extraction (multiple outcomes, subgroups): delegate to 2-3
  extraction agents in parallel, each handling a variable group
- Quality review: always delegate to critique agent after extraction
</effort_guidelines>

<task>
{{TASK_DESCRIPTION}}
</task>

Analyze the task, determine the optimal decomposition strategy, and
delegate to the appropriate agents. After receiving results, synthesize
into a unified extraction and ensure all quality checks pass.
```

### 10.5 Domain Expert Consultation Template

```xml
<role>
You are a pharmacogenetics domain expert providing guidance on data
extraction decisions that require specialized knowledge.
</role>

<expertise>
- CPIC guideline interpretation
- Genotype-phenotype mapping
- Drug metabolism pathway classification
- Clinical significance assessment
- Statistical methodology for pharmacogenetic studies
</expertise>

<task>
The extraction pipeline has flagged the following items for expert review:

{{FLAGGED_ITEMS}}

For each item, provide:
1. Your expert assessment
2. The correct classification or value
3. Your reasoning with references to relevant guidelines or standards
4. Confidence level in your assessment
</task>
```

---

## 11. Sources

### Anthropic Official Documentation

- [Prompting Best Practices (Claude 4.x)](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices) -- The single reference for prompt engineering with Claude Opus 4.6, Sonnet 4.6, and Haiku 4.5
- [Prompt Engineering Overview](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview) -- Entry point for Anthropic's prompt engineering documentation
- [Multishot Prompting Guide](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/multishot-prompting) -- Official guidance on few-shot/multishot examples
- [Structured Outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) -- JSON outputs and strict tool use documentation
- [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) -- Anthropic's guide to agent architectures and design patterns
- [Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system) -- How Anthropic built their orchestrator-worker research system
- [Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) -- Context management principles for agentic systems

### Scientific LLM Extraction Research

- [Collaborative LLMs for Automated Data Extraction in Living Systematic Reviews](https://pmc.ncbi.nlm.nih.gov/articles/PMC11469465/) (PMC, 2025) -- Two-reviewer LLM workflow achieving 0.94 accuracy on concordant extractions
- [AI-Assisted Data Extraction with a Large Language Model](https://www.acpjournals.org/doi/10.7326/ANNALS-25-00739) (Annals of Internal Medicine, 2025) -- 77.2% concordance between LLM-assisted and human-only extraction
- [LLM-IE: Python Package for Biomedical Generative Information Extraction](https://pmc.ncbi.nlm.nih.gov/articles/PMC11901043/) (PMC, 2025) -- Framework for biomedical NER, attribute extraction, and relation extraction with LLMs
- [LLMs Tackle Meta-analysis: Automating Scientific Hypothesis Generation](https://link.springer.com/chapter/10.1007/978-981-96-8912-5_2) (Springer, 2025) -- Decomposed zero-shot prompting for automated meta-analysis
- [Benchmarking LLMs for Pharmacogenomic Recommendations](https://www.nature.com/articles/s41397-025-00383-0) (Nature Pharmacogenomics Journal, 2025) -- LLM accuracy on CPIC guideline replication
- [Large Language Models for Conducting Systematic Reviews](https://www.sciencedirect.com/science/article/pii/S0895435625000794) (Scoping Review, 2025) -- LLMs cover 10 of 13 defined SR steps

### LLM-as-Judge and Evaluation

- [LLM-As-Judge: 7 Best Practices & Evaluation Templates](https://www.montecarlodata.com/blog-llm-as-judge/) -- Practical templates for evaluation prompts with bias mitigation
- [LLM Evaluation in 2025: Metrics, RAG, LLM-as-Judge](https://medium.com/@QuarkAndCode/llm-evaluation-in-2025-metrics-rag-llm-as-judge-best-practices-ad2872cfa7cb) -- Comprehensive evaluation methodology overview
- [Using LLMs for Evaluation](https://cameronrwolfe.substack.com/p/llm-as-a-judge) -- Cameron Wolfe's technical deep-dive on LLM judge design

### Agent Architecture and Prompt Design

- [Prompt Engineering for AI Agents](https://www.prompthub.us/blog/prompt-engineering-for-ai-agents) -- Practical patterns from production agents (Cline, Bolt.new)
- [A Practical Guide for Designing Production-Grade Agentic AI Workflows](https://arxiv.org/html/2512.08769v1) (arXiv, 2025) -- Comprehensive framework for agentic system design
- [Multi-Agent Design: Optimizing Agents with Better Prompts and Topologies](https://arxiv.org/abs/2502.02533) (arXiv, 2025) -- MASS framework for multi-agent prompt optimization
- [Sub-Agents in LLM Systems: Architecture, Execution Model, and Design Patterns](https://martinuke0.github.io/posts/subagents/) -- Detailed sub-agent architecture guide

### Few-Shot Prompting

- [Few-Shot Prompting Guide](https://www.promptingguide.ai/techniques/fewshot) -- Comprehensive technical reference
- [The Few Shot Prompting Guide](https://www.prompthub.us/blog/the-few-shot-prompting-guide) -- Practical decision framework
- [Zero-Shot vs Few-Shot Prompting](https://vellum.ai/blog/zero-shot-vs-few-shot-prompting-a-guide-with-examples) -- Comparative analysis with decision criteria
