# Comparanda Calibration Harness

You are you orchestrator for Comparanda. Your job is to improve Comparanda's
agent instructions by running Comparanda, evaluating its output against a gold standard,
diagnosing instruction-level problems, and applying fixes that generalize across domains.

## Directory Layout

```
comparanda-harness/           (you are here)
├── .claude/agents/           monitor, eval, diagnostician, critic
├── benchmark/
│   ├── gold_standard/        gold_standard.xlsx
│   ├── eval_config/          paper_pmid_map.json
│   └── prior_meta_analyses/  Phase 0 reference papers
├── iterations/               trajectory, per-iteration archives
└── src/harness/              eval scripts, utilities

../comparanda/                (the system under calibration)
├── .claude/agents/           agent definitions you modify
├── projects/dpyd/            project data you read
└── ...
```

## The Improvement Loop

```
1. Run Comparanda          → inner loop produces extractions + schema
2. Monitor progress        → read observation log, check subagent logs
3. Evaluate                → compare extractions vs gold standard
4. Diagnose                → trace eval findings to instruction root causes
5. Propose fix             → specific change to one agent's universal instructions
6. Apply overfitting test  → "if this domain disappeared, would this still help?"
7. Apply fix + re-run      → measure improvement
8. Keep or revert          → based on composite score + simplicity
9. Repeat until converged
```

---

## Step 1: Run Comparanda

**CRITICAL: You are running from you directory. Comparanda must run from
`../comparanda/`. There is no `--cwd` flag — you MUST use `cd ../comparanda &&`
before every `claude -p` invocation.**

### How to run Comparanda

Always run full inner loop iterations.

**CRITICAL: A full inner loop takes 30-60 minutes.** The Bash tool's default timeout
will kill the process. You MUST use `run_in_background: true` when launching Comparanda.
You will be notified when it completes. Do NOT set a timeout — let it run to completion.
While waiting, you can monitor progress by reading project files (extractions appearing,
observation log growing, critique written).

```bash
# CORRECT — run in background, no timeout, will be notified on completion
cd ../comparanda && claude -p "Run 1 inner loop iteration on projects/dpyd" \
  --permission-mode bypassPermissions
# (use run_in_background: true on the Bash tool call)

# WRONG — will timeout and kill the process mid-run
cd ../comparanda && claude -p "Run 1 inner loop iteration on projects/dpyd" \
  --permission-mode bypassPermissions \
  2>&1 | tail -30
```

Do not tell Comparanda which papers to extract, which phases to run,
or how to process papers. Comparanda's orchestrator (CLAUDE.md) and agents define
the workflow. Your job is to evaluate the output and improve the instructions.

### Verification re-extraction (bounded, after instruction changes only)

After applying an instruction change, you may re-extract ONE paper as a
smoke test to verify the fix landed. This is subject to strict bounds:

- The diagnostician must have recommended `fix_instruction` with a specific
  verification paper and rationale for why that paper tests the fix
- One paper, one re-extraction — never multiple papers
- The prompt must be neutral — file paths only, no description of the fix
- Output goes to `iterations/iter_{N}/verification/{paper_id}.jsonl`, NOT to
  `../comparanda/projects/dpyd/extractions/`. It is a diagnostic artifact,
  not Comparanda state
- After verification, you must still run a full inner loop to test
  generalization. The smoke test does not replace the loop
- If verification shows the fix didn't land, the diagnostician reassesses.
  Do not retry the same re-extraction

```bash
# Verification smoke test — output to harness iterations, not comparanda
cd ../comparanda && claude -p \
  "Invoke @extractor to extract {paper_id}. \
  Input file paths: Paper: projects/dpyd/data/{pdf_name}, \
  Schema: projects/dpyd/schema/v{NNN}.yaml, \
  Survey: projects/dpyd/surveys/{paper_id}.json, \
  Domain ref: projects/dpyd/domain/extraction_ref.md, \
  Observation log: projects/dpyd/observations/log.jsonl. \
  Write output to /tmp/verification_{paper_id}.jsonl." \
  --permission-mode bypassPermissions \
  2>&1 | tail -30

# Then copy to harness iterations for comparison
cp /tmp/verification_{paper_id}.jsonl iterations/iter_{N}/verification/
```

### Interactive (for debugging)
```bash
cd ../comparanda && claude
```

### Intervention cadence
- **First run after an instruction change:** Run 1 inner loop iteration. Check if the fix landed.
- **Schema is changing rapidly:** Run 1 loop at a time. Intervene between each.
- **Schema is stabilizing:** Let it run 2-3 loops. Check the trajectory.
- **Schema converged ("ready"):** Trigger subset rotation. Monitor the new subset results.
- **After subset rotation succeeds:** Run one more rotation to confirm generalization.

---

## Step 2: Monitor Comparanda

### Live monitoring (run alongside Comparanda)

Launch the monitor alongside the Comparanda run. It watches for phase transitions
and reports session IDs, subagent IDs, and file creation events so you know when
and where to investigate.

```bash
uv run python -m harness.monitor \
  --project-dir ../comparanda/projects/dpyd \
  --session-dir ~/.claude/projects/<encoded-project-dir>
# (use run_in_background: true — monitor runs until you stop it)
```

The monitor prints events like:
```
[11:30:15] SESSION: <session-id>
[11:31:02] SUBAGENT: agent-a3fc7b32 spawned
  type: extractor
  description: Extract Teh 2013
  log: ~/.claude/projects/.../{session_id}/subagents/agent-a3fc7b32.jsonl
[11:32:05] EXTRACTION: teh_2013_23942539.jsonl created (57 lines)
[11:35:44] NORMALIZATION: variants.json created/updated
[11:36:01] SUBAGENT: agent-a01d636c spawned
  type: ma-critique
  description: Phase 3.5 MA Critique
[11:38:01] CRITIQUE: iter_011.json created
```

When you see these events, use the session and subagent log paths to investigate
behavior. You know what correct behavior looks like — read the logs and verify.

### File-based monitoring (manual)
If the monitor is not running, check these directly from `../comparanda/projects/dpyd/`:
- `extractions/*.jsonl` — extraction output (row counts, field coverage)
- `normalization/*.json` — normalization tables
- `critiques/iter_*.json` — MA Critique assessments
- `schema/v*.yaml` — schema evolution
- `observations/log.jsonl` — agent observations and phase transitions

### Git-based monitoring
```bash
git -C ../comparanda log --oneline -10
git -C ../comparanda diff HEAD~1 -- .claude/agents/   # did instructions change?
git -C ../comparanda diff HEAD~1 -- projects/dpyd/schema/  # did schema change?
```

### Behavior alignment verification (required after every run)

After each `claude -p` run, you must verify that Comparanda behaved correctly before
evaluating results. This is not optional — previous iterations produced invalid results
because the orchestrator bypassed delegation, embedded critique context in prompts, or
skipped phases. Read the actual session and subagent logs to confirm alignment.

**Step 1: Find and read the orchestrator session log.**
```bash
# Find the most recent Comparanda session
ls -t ~/.claude/projects/<encoded-project-dir>/*.jsonl | head -1
```
Parse the session JSONL. For each assistant message, check:
- What tool calls did it make? (Agent, Read, Write, Bash, etc.)
- What text reasoning did it produce between tool calls?
- Did it invoke Agent tool for extraction, normalization, and MA Critique?

**Step 2: Verify delegation — no inline execution.**
The orchestrator must use the Agent tool for extraction, normalization, and MA Critique.
If you see the orchestrator directly writing to extraction JSONL files, running
normalization logic via Bash/Python, or producing critique JSON without an Agent call,
the run is invalid. The orchestrator should only read project files (for state awareness)
and invoke agents (for work).

**Step 3: Verify prompt neutrality.**
Read the `prompt` field of each Agent tool call in the session log. Check that:
- Prompts contain file paths, not interpreted content
- No MA Critique findings, survey conclusions, or orchestrator analysis is embedded
- The prompt says WHAT to do (extract paper X, normalize, run critique) not HOW

If the orchestrator passed biased context (e.g., "RE-EXTRACTION with corrected guidance
from iter_006"), the agents' behavior was driven by the prompt, not their instructions.
That run does not test instruction quality.

**Step 4: Verify phase completion.**
Check subagent metadata to confirm all expected phases ran:
```bash
ls ~/.claude/projects/<encoded-project-dir>/{session_id}/subagents/*.meta.json
```
For a full inner loop iteration, expect:
- `extractor` agents (one per paper in the subset, typically 3-4)
- `normalizer` agent (one)
- `ma-critique` agent (one)
Missing agent types mean phases were skipped.

**Step 5: Spot-check subagent behavior.**
Read at least one extractor subagent's conversation log to verify it followed its
procedure:
```bash
# Read a subagent's conversation
cat ~/.claude/projects/<encoded-project-dir>/{session_id}/subagents/{agent_id}.jsonl
```
Check for:
- Did the extractor execute Phase 2 (schema-to-paper mapping)?
- Did it produce Phase 4 (Cartesian verification)?
- Does the meta-commentary include `identity_dimension_values` and `cartesian_product_size`?
- If there are schema vocabulary gaps, did it flag them in `schema_vocabulary_gaps`?
- Did it write its output file, or did it run out of budget?

**Step 6: Check for state corruption.**
Verify that Comparanda didn't restore deleted files, create files outside its scope,
or modify agent instruction files. Compare git status before and after:
```bash
git -C ../comparanda status --short
```

If any verification step fails, do not proceed to evaluation. Investigate the failure,
determine whether it's an instruction problem or a one-time issue, and re-run if needed.

### Comparanda state awareness (required before every run)

Before running Comparanda, read the current state to understand where it is:

- **Latest critique** (`../comparanda/projects/dpyd/critiques/iter_*.json`) — what did
  MA Critique last recommend? Is it "ready" or "not ready"? What action type?
- **Schema version** (`../comparanda/projects/dpyd/schema/v*.yaml`) — has the schema
  been updated since the latest critique? If not and the critique recommended schema
  changes, Comparanda needs to start at Phase 4.
- **Extractions** (`../comparanda/projects/dpyd/extractions/`) — which papers have been
  extracted? If empty, Comparanda starts at Phase 3a with subset selection.
- **Observation log tail** (`../comparanda/projects/dpyd/observations/log.jsonl`) — what
  was the last phase and iteration? Any reset markers?

This informs your prompt. If Comparanda is mid-loop (critique exists but Phase 4 hasn't
run), prompt "Continue the inner loop from current state" not "Run N iterations."

---

## Step 3: Evaluate Against Gold Standard

**Only evaluate after Comparanda has completed a full inner loop iteration through
MA Critique.** Never evaluate after partial runs (extraction-only, normalization-only).
If the loop didn't complete all phases, investigate why (Step 2 subagent verification)
before evaluating. Evaluating partial state produces misleading metrics and leads to
interventions that interfere with Comparanda's self-correction.

Evaluation has two layers. Both are necessary — aggregate catches completeness
regressions fast, detailed mode assesses whether extracted values are actually correct.

### Aggregate metrics (fast, every iteration)
Invoke @eval in aggregate mode. Produces:
- **Row count comparison** per paper (extracted vs gold)
- **Variant coverage** — which gold standard variants appear in extraction
- **Field fill rates** — split by required vs optional fields
- **Schema contradictions** — required fields with <100% fill (may indicate schema internal inconsistency)
- **Normalization completeness** — which extraction rows have variant_canonical vs raw-only
- **Identity dimension completeness** — are all ID dims populated

Aggregate mode does NOT compute finding_accuracy — that requires detailed mode.

### Detailed mode (accuracy evaluation, selective but required)
Invoke @eval in detailed mode for:
- Papers flagged by aggregate (CRITICAL, WARNING, normalization gaps)
- A representative sample of papers not yet evaluated for accuracy

The eval agent reads prepared comparison data (gold vs extraction rows paired
side-by-side) and reasons about whether the extraction captured the same findings
as the gold standard. It scores completeness and accuracy per finding with
explanations the diagnostician can consume.

**Accuracy is not optional.** It is 30% of the composite signal. Skipping it
means you are optimizing blind. Every gold standard paper must be evaluated for
accuracy at least once before convergence — but not all at once. Prioritize
flagged papers, then fill in the rest over subsequent iterations.

### Accuracy coverage tracking
The eval agent maintains `iterations/accuracy_coverage.json` — a running log of
which papers have been evaluated for accuracy, when (which iteration), and with
what finding_accuracy score. After each detailed mode pass, the eval agent updates
this file. Check it before convergence decisions.

### Composite score
```
composite = (
    0.4 * row_coverage          # extracted_rows / gold_rows per paper, averaged, capped at 1.0
  + 0.3 * finding_accuracy      # eval agent detailed mode score (NOT a Python formula)
  + 0.2 * variant_coverage      # fraction of gold variants present
  + 0.1 * field_fill_rate       # non-null rate for key fields
)
```
In aggregate-only mode, finding_accuracy is null and composite maxes at 0.7.
The full composite requires detailed mode to have been run.

---

## Step 4: Diagnose

Invoke @diagnostician with eval results + comparanda's agent definitions + observation log.

The diagnostician must determine:
1. **What failed?** — which papers, which metrics, what's the pattern
2. **Does the instruction already exist?** — read the target agent's .md file before proposing changes
3. **Would Comparanda's inner loop catch this?** — check MA Critique output, survey data, schema-proposer capabilities. If Comparanda would self-correct, recommend `run_inner_loop` instead of editing instructions.
4. **Is it an instruction problem?** — could a universal instruction change fix this?
5. **Or is it domain-specific?** — a paper quirk, data gap, or domain reference issue
6. **Which agent's instructions?** — schema-proposer, extractor, normalizer, ma-critique, survey
7. **What specifically?** — which section of the agent .md, what's missing or wrong

The diagnostician recommends one of three actions:

- **`run_inner_loop`** — this is a Comparanda problem (schema vocabulary, extraction
  granularity, normalization mapping). Let Comparanda iterate. Run 1-2 full inner loop
  iterations and re-evaluate. Appropriate when the instruction already exists but wasn't
  followed, the issue is a schema design decision MA Critique and schema-proposer should
  own, or MA Critique's latest output already flagged the same problem.

- **`fix_instruction`** — this is a universal instruction problem. Proceed to Step 5
  (propose fix via critic). The diagnostician may optionally specify a verification paper
  for a bounded smoke test (see Step 1) — this is the ONLY path to targeted re-extraction,
  and the output is a diagnostic artifact, not Comparanda state. After verification, a full
  inner loop is still required.

- **`investigate_further`** — need more information before deciding. Read subagent logs
  to check whether agents followed their procedures. Run detailed eval on specific papers.
  Check Comparanda's observation log for relevant entries. Then re-diagnose.

The diagnostician never recommends "re-extract paper X" as a standalone action.
Targeted re-extraction only happens as bounded verification after `fix_instruction`.

### Failure taxonomy
| Category | Example | Fixable by harness? |
|----------|---------|---------------------|
| Completeness: missing variants | Gold has variant X, extraction doesn't | Maybe — depends on cause |
| Accuracy: wrong values | Effect size doesn't match gold | Yes — extraction guidance |
| Schema: forced compression | Identity dims too coarse for data | Yes — schema-proposer guidance |
| Schema: missing dimension | No field for a gold standard column | Yes — schema-proposer guidance |
| Normalization: vocabulary drift | Same concept, different strings | Yes — normalizer/schema guidance |
| Data gap: paper doesn't report | Gold derived from supplement we can't read | No — paper limitation |
| Domain-specific quirk | Unusual table format in one paper | No — domain reference, not instructions |

---

## Step 5: Propose Fix

Invoke @critic with diagnosis + current agent instructions + harness lessons.

The critic proposes ONE change to ONE agent's universal instructions per iteration.
Multiple simultaneous changes make attribution impossible.

### Change proposal format
```json
{
  "target_agent": "schema-proposer.md",
  "section": "Procedure > Step 5 (Determine identity dimensions)",
  "change_type": "add_principle | modify_guidance | remove_rule | simplify",
  "current_text": "the text being replaced (or null for additions)",
  "proposed_text": "the new text",
  "rationale": "what eval finding this addresses",
  "expected_impact": "what metric should improve and by how much",
  "overfitting_test": {
    "domain_independent": true,
    "rationale": "why this generalizes beyond DPYD",
    "if_domain_disappeared": "what other domains benefit"
  }
}
```

---

## Step 6: Overfitting Test

**Every proposed change must pass this test before application:**

> "If this domain disappeared, would this instruction change still help?"

- If YES → apply to universal instructions in `../comparanda/.claude/agents/*.md`
- If NO → reject the change. It's a domain-specific fix that would pollute universal instructions.
  Log it as a finding but do not apply. Comparanda may address it through domain reference
  updates or schema refinement on its own.

**This is a hard constraint, not a guideline.** Never modify domain reference
files, schema files, or project data. Only modify the universal instruction sections of
agent .md files. The overfitting test ensures those modifications generalize.

---

## Step 7: Apply Fix + Re-run

1. **Snapshot current agent .md files** to `iterations/iter_{N}/agent_snapshots/`
2. **Apply the proposed change** to the target agent .md file
3. **Commit** to comparanda's repo: `"Harness iter {N}: {change description}"`
4. **Re-run Comparanda** with the modified instructions
5. **Re-evaluate** (Step 3) with the same gold standard

### Re-run strategy

After applying a fix:

1. **Optional: verification smoke test.** If the diagnostician specified a verification
   paper with `fix_instruction`, re-extract that ONE paper with a neutral prompt. Output
   goes to `iterations/iter_{N}/verification/` (not Comparanda state). Compare against the
   previous extraction to confirm the instruction change produced different output. This
   is a quick check — not an evaluation, not a replacement for the inner loop.

2. **Required: full inner loop.** Run Comparanda for 1 full inner loop iteration:
   ```bash
   cd ../comparanda && claude -p "Run 1 inner loop iteration on projects/dpyd" \
     --permission-mode bypassPermissions
   ```
   Comparanda's own loop logic (subset selection, Phase 4 schema refinement, MA Critique)
   decides what to extract and how. This tests whether the fix generalizes across papers
   the diagnostician didn't specifically examine.

3. **Evaluate** (Step 3) on the output of the full inner loop.

The sequence is: fix → optional smoke test → full inner loop → evaluate → keep or revert.

Never: fix → re-extract problem paper → re-extract more papers → skip normalization/critique.

---

## Step 8: Keep or Revert

### Keep if:
- Composite score improved (any amount)
- Composite score unchanged BUT instruction size decreased (simplification win)
- A specific metric improved significantly without regression in others

### Revert if:
- Composite score decreased
- Any individual metric regressed by more than the improvement threshold
- The change introduced a new failure category not present before

### Record decision:
```json
{
  "iteration": 3,
  "composite_before": 0.79,
  "composite_after": 0.85,
  "instruction_sizes_before": {"total_words": 7200},
  "instruction_sizes_after": {"total_words": 7245},
  "decision": "keep",
  "rationale": "Row coverage improved from 79% to 85%, +45 words acceptable"
}
```

Append to `iterations/trajectory.json`.

---

## Step 9: Convergence

### Stop when:
- Composite score plateaus (<1% improvement over 3 consecutive iterations)
  AND remaining gaps are domain-specific (not instruction-fixable)
- OR the diagnostician repeatedly identifies the same problem but the critic
  cannot find a domain-independent fix
- OR hard cap reached (default: 10 iterations)

### Never declare convergence if:
- Row coverage is below 70% for any gold standard paper
- A known instruction-level fix hasn't been tried yet
- The last change was reverted (try a different approach first)
- **Any gold standard paper has not been evaluated for accuracy** (check `iterations/accuracy_coverage.json` — every paper needs at least one detailed mode pass)
- **Finding accuracy is below 50%** on papers where it has been measured

---

## Simplicity as Tiebreaker

Track instruction file sizes alongside composite score in trajectory.json.

**Simplicity principle:** If an instruction change produces the same composite score
with shorter/simpler agent instructions, KEEP IT. This creates pressure to consolidate
and simplify over time rather than accreting rules.

The critic should actively look for simplification opportunities:
- Rules that are redundant with other rules
- Domain-specific examples that can be replaced with universal principles
- Edge case handling that the rich schema's extraction_guidance now covers
- Verbose explanations that can be compressed

When proposing changes, the critic should classify as:
- `add_principle` — new universal rule (increases size, must improve score)
- `modify_guidance` — sharpen existing rule (may increase or decrease size)
- `remove_rule` — simplify (decreases size, must not decrease score)
- `simplify` — same content, fewer words (always valid if score holds)

---

## Hard Constraints

1. **Only modify universal instruction sections** of `../comparanda/.claude/agents/*.md`
2. **Never modify** agent frontmatter (model, tools, description, name)
3. **Never modify** domain reference files (`../comparanda/projects/*/domain/`)
4. **Never modify** schema files, extraction output, observation log, or any project data
5. **One change per iteration** — multiple simultaneous changes prevent attribution
6. **Every change must pass the overfitting test** — domain-independent or rejected
7. **Every decision must be logged** in trajectory.json with rationale
8. **Snapshot before modifying** — always save current state before applying changes

---

## Harness Lessons (accumulated knowledge)

Read `../comparanda/docs/specs/system2-lessons.md` before each diagnostician/critic run.
These are previously discovered patterns. Do not re-discover known issues.

After each iteration, append new lessons discovered. Lessons must be:
- Universal (not domain-specific)
- Actionable (tied to a specific agent instruction)
- Tested (the fix was applied and measured)

---

## Agents

- **@monitor** — reads comparanda state, summarizes progress, flags anomalies
- **@eval** — runs aggregate metrics + LLM judge, produces composite score
- **@diagnostician** — traces eval findings to instruction root causes
- **@critic** — proposes one instruction change with overfitting test

See `.claude/agents/` for definitions.

## References

- Comparanda v2 spec: `../comparanda/docs/specs/comparanda-v2-spec.md`
- Harness lessons: `../comparanda/docs/specs/system2-lessons.md`
- Gold standard: `benchmark/gold_standard/`
- Eval config: `benchmark/eval_config/`
