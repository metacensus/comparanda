from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_qc_repair_uses_resume_not_fresh_reinvoke() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    qc_section = text.split("### Phase 3a-QC — Extraction QC", 1)[1].split(
        "### Phase 3b — Normalization", 1
    )[0]

    assert "SendMessage" in qc_section
    assert "extractor_sessions.json" in qc_section
    assert "You MUST first resume the original extractor" in qc_section
    assert "retry resume up to 2 more times" in qc_section
    assert "instantiate a NEW extractor" in qc_section
    assert "Do not use `continue` as the repair fallback" in qc_section
    assert "re-invoke @extractor" not in qc_section


def test_orchestrator_records_extractor_registry() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    phase_3a = text.split("### Phase 3a — Extraction", 1)[1].split(
        "### Phase 3a-QC — Extraction QC", 1
    )[0]

    assert "extractor_session_registry.py upsert" in phase_3a
    assert "returned extractor agent/subagent ID" in phase_3a


def test_extractor_prompt_points_to_sdk_contract_before_internals() -> None:
    text = (REPO_ROOT / ".claude" / "agents" / "extractor.md").read_text()
    contract_idx = text.index("docs/extraction-sdk-agent-contract.md")
    internals_idx = text.index("scripts/extraction_sdk.py")

    assert contract_idx < internals_idx
    assert "scripts/run_extraction_build.py" in text
    assert "resume repair mode" in text


def test_qc_prompt_requires_machine_readable_repair_packet() -> None:
    text = (REPO_ROOT / ".claude" / "agents" / "extraction-qc.md").read_text()

    assert '"action_class"' in text
    assert '"must_fix_before_normalization"' in text
    assert '"repair_packet"' in text
    for action_class in (
        "blocking_reextract",
        "targeted_extraction_repair",
        "schema_or_normalization_note",
        "nonblocking_note",
    ):
        assert action_class in text
