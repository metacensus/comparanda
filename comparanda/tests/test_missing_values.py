"""Missingness must survive real builder/CLI paths without disguising source gaps."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_extraction_sdk import write_fixture
from scripts.extraction import extraction_sdk as sdk

core = sdk.core
ev = sdk.ev
REPO = Path(__file__).resolve().parents[1]


def setup_builder(tmp_path: Path):
    project, bundle, schema = write_fixture(tmp_path)
    results = bundle / "sections/results.md"
    results.write_text(results.read_text() + "\nExact values are in Supplemental Table S1. All tested variants were in equilibrium. No measurements were collected for the secondary outcome.")
    builder = sdk.ExtractionBuilder(project_dir=project, paper_id="paper_1", schema_path=schema, bundle_dir=bundle)
    quote = builder.evidence.quote("sections/results.md").exact("Entity A group A had response in 3 of 10 patients.")
    row = {
        "pmid": sdk.paper(111, builder.evidence.metadata("paper_id")),
        "entity_raw": sdk.paper("Entity A", quote),
        "outcome_raw": sdk.paper("response", quote),
        "comparison_group_raw": sdk.paper("group A", quote),
        "n_total": sdk.paper(10, quote),
    }
    return builder, row


def add_row(builder, row):
    with builder.chunk("results") as chunk:
        chunk.add_row(row)
    return builder._rows_payload_for_chunks()


def test_single_helper_has_no_null_or_abstention_aliases():
    assert not hasattr(sdk, "null")
    assert not hasattr(sdk, "abstained")
    with pytest.raises(TypeError):
        sdk.missing(explanation="A reason must be chosen explicitly.")
    for reason in ("unknown_label", [], 0):
        with pytest.raises(ValueError):
            sdk.missing(reason, "Explanation")
    for explanation in ("", "   ", None):
        with pytest.raises(ValueError):
            sdk.missing("not_found", explanation)


@pytest.mark.parametrize("reason", sorted(ev.MISSING_REASONS))
def test_all_reasons_survive_staging_and_finalization(tmp_path, reason):
    builder, row = setup_builder(tmp_path)
    quote = builder.evidence.quote("sections/results.md").exact("No measurements were collected for the secondary outcome.")
    sources = [quote] if reason in ev.MISSING_REASONS_REQUIRING_SOURCES else []
    row["n_total"] = sdk.missing(reason, "A specific explanation for this unavailable value.", sources)
    add_row(builder, row)
    assert builder.validate() == []
    out = builder.project_dir / "extractions/paper_1.jsonl"
    builder.finalize(out)
    written = json.loads(out.read_text().splitlines()[0])
    assert written["n_total"] is None
    assert written["provenance"]["n_total"] == {
        "missing_reason": reason,
        "reasoning": "A specific explanation for this unavailable value.",
        "sources": [quote.as_source()] if sources else [],
    }
    report = json.loads((builder.staging_dir / "validation/final.validate.json").read_text())
    assert report["missingness"]["by_reason"][reason] == 1
    assert report["warning_count"] == 0
    # Nonmissing data/provenance remain byte-for-byte equal after parsing.
    for field in ("entity_raw", "outcome_raw", "comparison_group_raw"):
        assert written[field] == row[field].value
        expected = row[field].to_entry(field)
        assert written["provenance"][field] == {k: v for k, v in expected.items() if k not in ("name", "value")}


def test_fallback_is_counted_by_occurrence_and_distinct_explanation(tmp_path):
    builder, row = setup_builder(tmp_path)
    row["n_total"] = sdk.missing(None, "No named category fits this explicitly described edge case.")
    add_row(builder, row)
    payload = builder._rows_payload_for_chunks()
    payload["rows"].append(payload["rows"][0])
    report = ev.missingness_report(payload, builder.schema)
    assert report["missingness"]["missing_field_count"] == 2
    assert report["missingness"]["uncategorized_count"] == 2
    assert report["missingness"]["uncategorized_fraction"] == 1
    assert report["missingness"]["uncategorized_distinct_explanations"] == 1
    assert [w["row"] for w in report["warnings"]] == [0, 1]
    assert all(w["field"] == "n_total" for w in report["warnings"])
    builder.finalize(builder.project_dir / "extractions/paper_1.jsonl")
    text = (builder.staging_dir / "validation/final.validate.txt").read_text()
    assert "WARNING" in text and "uncategorized: 1" in text


@pytest.mark.parametrize("bad", [None, {"value": None}, {"value": None, "reasoning": "Explained but category omitted"}, {"value": None, "extraction_certainty": "abstained", "reasoning": "Old shortcut", "sources": []}])
def test_bare_null_and_old_payloads_are_rejected_without_overwriting_output(tmp_path, bad):
    builder, row = setup_builder(tmp_path)
    row["n_total"] = bad
    add_row(builder, row)
    out = builder.project_dir / "extractions/paper_1.jsonl"
    out.write_text("preserved original\n")
    with pytest.raises(core.ExtractionValidationError):
        builder.finalize(out)
    assert out.read_text() == "preserved original\n"


def test_automatic_fill_null_cannot_bypass_missingness(tmp_path):
    builder, row = setup_builder(tmp_path)
    del row["n_total"]
    add_row(builder, row)
    failures = builder.validate()
    assert any(f.code == "unexplained_missing_field" and "n_total" in f.location for f in failures)


def test_normalization_placeholder_exemption_and_zero(tmp_path):
    builder, row = setup_builder(tmp_path)
    schema_text = builder.schema_path.read_text() + "\n  - name: entity_id\n    type: string\n    normalization: entities\n"
    builder.schema_path.write_text(schema_text)
    builder.schema = ev.Schema.load(builder.schema_path)
    zero = builder.bundle_dir / "sections/zero.md"
    zero.write_text("No events: 0.")
    builder.evidence = sdk.EvidenceStore(builder.bundle_dir)
    row["n_total"] = sdk.paper(0, builder.evidence.quote("sections/zero.md").exact("No events: 0."))
    add_row(builder, row)
    out = builder.project_dir / "extractions/paper_1.jsonl"
    builder.finalize(out)
    written = json.loads(out.read_text().splitlines()[0])
    assert written["entity_id"] is None
    assert "entity_id" not in written["provenance"]
    assert written["n_total"] == 0
    assert "missing_reason" not in written["provenance"]["n_total"]


@pytest.mark.parametrize("reason", sorted(ev.MISSING_REASONS_REQUIRING_SOURCES))
def test_evidence_required_for_positive_missingness_claims(tmp_path, reason):
    builder, row = setup_builder(tmp_path)
    row["n_total"] = sdk.missing(reason, "A reason without any support.")
    add_row(builder, row)
    assert any(f.code == "missingness_sources_required" for f in builder.validate())


def test_missing_supplement_requires_available_reference_not_invented_quote(tmp_path):
    builder, row = setup_builder(tmp_path)
    row["n_total"] = sdk.missing("referenced_source_unavailable", "Table S1 is not in the bundle.", [{"location": "tables/table_s1.md", "page": 2, "quote": "Invented missing contents"}])
    add_row(builder, row)
    assert any(f.code == "location_not_a_bundle_file" for f in builder.validate())
    entry = row["n_total"].to_entry("n_total")
    entry["sources"] = [{"location": "sections/results.md", "page": 4, "quote": "A fabricated source statement"}]
    bundle = ev.BundleSources.load(builder.bundle_dir)
    assert any(f.code == "quote_not_at_location" for f in ev._validate_field_entry(entry, schema=builder.schema, row_loc="row[0]", bundle=bundle))


def test_unreadable_figure_can_use_real_location_without_fake_quote(tmp_path):
    builder, row = setup_builder(tmp_path)
    figures = builder.bundle_dir / "figures"
    figures.mkdir()
    (figures / "plot.png").write_bytes(b"fixture presence only; no image decoding")
    row["n_total"] = sdk.missing("unreadable", "The label is illegible.", [{"location": "figures/plot.png", "page": 4}])
    add_row(builder, row)
    assert builder.validate() == []


def test_qualitative_finding_and_missing_number_survive_separately(tmp_path):
    builder, row = setup_builder(tmp_path)
    quote = builder.evidence.quote("sections/results.md").exact("Exact values are in Supplemental Table S1.")
    row["n_total"] = sdk.missing("referenced_source_unavailable", "Exact values are unavailable because Supplemental Table S1 is missing.", [quote])
    builder.meta({"qualitative_findings": [{"finding": "All tested variants were in equilibrium.", "sources": [{"location": "sections/results.md", "page": 4, "quote": "All tested variants were in equilibrium."}]}]})
    add_row(builder, row)
    out = builder.project_dir / "extractions/paper_1.jsonl"
    builder.finalize(out)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows[0]["n_total"] is None
    assert rows[0]["provenance"]["n_total"]["missing_reason"] == "referenced_source_unavailable"
    assert rows[-1]["qualitative_findings"][0]["finding"] == "All tested variants were in equilibrium."


def test_cli_patch_replaces_stale_provenance_and_preserves_reviewed_values(tmp_path):
    builder, row = setup_builder(tmp_path)
    add_row(builder, row)
    out = builder.project_dir / "extractions/paper_1.jsonl"
    builder.finalize(out)
    prior = builder.project_dir / "extractions/prior.jsonl"
    prior.write_bytes(out.read_bytes())
    identity = [111, "Entity A", "response", "group A"]
    patch = {"schema_version": "v001", "identity_dimensions": builder.schema.identity_dimensions, "rows": [{"identity": identity, "fields": [sdk.missing("not_found", "The revised source has no supported denominator.").to_entry("n_total")]}]}
    patch_file = builder.staging_dir / "patch.json"
    patch_file.write_text(json.dumps(patch))
    meta = builder.staging_dir / "patch-meta.json"
    meta.write_text(json.dumps({"sources_used": [], "identity_dimension_values": {}, "schema_diff_applied": {}}))
    command = [sys.executable, str(REPO / "scripts/extraction/write_extraction.py"), "itern", "--paper-id", "paper_1", "--schema", str(builder.schema_path), "--bundle", str(builder.bundle_dir), "--out", str(out), "--prior", str(prior), "--rows-file", str(patch_file), "--meta-file", str(meta)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    written = json.loads(out.read_text().splitlines()[0])
    assert written["n_total"] is None
    assert written["provenance"]["n_total"] == {"missing_reason": "not_found", "reasoning": "The revised source has no supported denominator.", "sources": []}
    assert written["provenance"]["entity_raw"] == json.loads(prior.read_text().splitlines()[0])["provenance"]["entity_raw"]
    original = json.loads(prior.read_text().splitlines()[0])
    original["provenance"]["n_total"]["reviewed_by"] = "reviewer"
    merged = core.merge_partial_into_prior(original, patch["rows"][0]["fields"])
    assert merged["n_total"] == 10
    assert merged["provenance"]["n_total"] == original["provenance"]["n_total"]
    # Replacing missing with a reported value clears the obsolete reason.
    restored = core.merge_partial_into_prior(written, [row["n_total"].to_entry("n_total")])
    assert restored["n_total"] == 10
    assert "missing_reason" not in restored["provenance"]["n_total"]


def test_builder_cli_finalization_keeps_missingness_audit_report(tmp_path):
    builder, row = setup_builder(tmp_path)
    row["n_total"] = sdk.missing(
        None,
        "This concrete edge case does not fit any named missingness category.",
    )
    add_row(builder, row)
    rows_path, meta_path = builder.write_staging()
    out = builder.project_dir / "extractions/paper_1.jsonl"
    command = [
        sys.executable,
        str(REPO / "scripts/extraction/extraction_builder.py"),
        "finalize",
        "--project", str(builder.project_dir),
        "--paper-id", "paper_1",
        "--schema", str(builder.schema_path),
        "--bundle", str(builder.bundle_dir),
        "--staging-dir", str(builder.staging_dir),
        "--out", str(out),
    ]
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert rows_path.exists() and meta_path.exists()

    written = json.loads(out.read_text().splitlines()[0])
    assert written["n_total"] is None
    assert written["provenance"]["n_total"] == {
        "missing_reason": None,
        "reasoning": "This concrete edge case does not fit any named missingness category.",
        "sources": [],
    }
    report = json.loads((builder.staging_dir / "validation/final.validate.json").read_text())
    assert report["ok"] is True
    assert report["missingness"]["missing_field_count"] == 1
    assert report["missingness"]["uncategorized_count"] == 1
    assert report["warning_count"] == 1
    assert report["warnings"] == [{
        "code": "uncategorized_missing",
        "row": 0,
        "field": "n_total",
        "explanation": "This concrete edge case does not fit any named missingness category.",
    }]
    assert "WARNING: QC must review" in (
        builder.staging_dir / "validation/final.validate.txt"
    ).read_text()
