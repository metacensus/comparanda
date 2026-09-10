from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.extraction.extraction_sdk import (
    ExtractionBuilder,
    EvidenceStore,
    derived,
    paper,
    source_component,
)


def write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "projects" / "example_domain"
    bundle = project / "data" / "processed" / "paper_1"
    schema = project / "schema" / "v001.yaml"
    (bundle / "sections").mkdir(parents=True)
    (bundle / "tables").mkdir()
    schema.parent.mkdir(parents=True)
    (project / "extractions").mkdir()

    (bundle / "manifest.json").write_text(json.dumps({
        "paper_id": "111",
        "sections": [
            {
                "path": "sections/results.md",
                "heading": "Results",
                "page_range": [4, 4],
            },
            {
                "path": "sections/methods.md",
                "heading": "Methods",
                "page_range": [2, 2],
            },
        ],
        "tables": [],
        "figures": [],
    }))
    (bundle / "sections" / "results.md").write_text(
        "Entity A group A had response in 3 of 10 patients. "
        "The complementary non-event count was 7 patients."
    )
    (bundle / "sections" / "methods.md").write_text(
        "This methods section does not contain the result quote."
    )

    schema.write_text(
        """
schema_version: v001
domain: example_domain
identity_dimensions:
  - field: pmid
  - field: entity_raw
  - field: outcome_raw
  - field: comparison_group_raw
fields:
  - name: pmid
    type: integer
  - name: entity_raw
    type: string
  - name: outcome_raw
    type: string
  - name: comparison_group_raw
    type: string
  - name: n_total
    type: integer
""".strip()
    )
    return project, bundle, schema


def test_evidence_exact_and_wrong_location_candidates(tmp_path: Path) -> None:
    _project, bundle, _schema = write_fixture(tmp_path)
    evidence = EvidenceStore(bundle)

    handle = evidence.quote("sections/results.md").exact("Entity A group A")
    assert handle.as_source()["location"] == "sections/results.md"
    assert handle.as_source()["page"] == 4

    with pytest.raises(ValueError) as exc:
        evidence.quote("sections/methods.md").exact("Entity A group A")
    assert "found elsewhere" in str(exc.value)
    assert "sections/results.md" in str(exc.value)


def test_builder_finalizes_and_persists_validation_report(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    out = project / "extractions" / "paper_1.jsonl"

    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    evidence = builder.evidence
    result_quote = evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    builder.common_fields({
        "pmid": paper(111, evidence.metadata("paper_id")),
    })
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "entity_raw": paper("Entity A", result_quote),
            "outcome_raw": paper("response", result_quote),
            "comparison_group_raw": paper("group A", result_quote),
            "n_total": paper(10, result_quote),
        })
        assert chunk.validate() == []
    builder.meta({
        "sources_used": [{"source": "sections/results.md", "role": "direct"}],
        "identity_dimension_values": {
            "pmid": [111],
            "entity_raw": ["Entity A"],
            "outcome_raw": ["response"],
            "comparison_group_raw": ["group A"],
        },
    })

    result = builder.finalize(out)
    assert result.row_count == 1
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["entity_raw"] == "Entity A"
    assert lines[0]["provenance"]["n_total"]["sources"][0]["location"] == "sections/results.md"

    report_path = project / "extractions" / ".staging" / "paper_1" / "validation" / "final.validate.json"
    report = json.loads(report_path.read_text())
    assert report["ok"] is True
    assert report["failure_count"] == 0

    manifest_path = project / "extractions" / ".staging" / "paper_1" / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["staging_script_path"].endswith(
        "projects/example_domain/extractions/.staging/paper_1/scripts/build.py"
    )
    assert manifest["finalized_jsonl_path"] == str(out)
    assert manifest["extractor_session"]["schema_version"] == "v001"
    assert manifest["extractor_session"]["finalized_jsonl_path"] == str(out)


def test_legacy_writer_cli_uses_shared_finalization_core(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    out = project / "extractions" / "legacy.jsonl"

    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    builder.common_fields({"pmid": paper(111, builder.evidence.metadata("paper_id"))})
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "entity_raw": paper("Entity A", quote),
            "outcome_raw": paper("response", quote),
            "comparison_group_raw": paper("group A", quote),
            "n_total": paper(10, quote),
        })
    builder.meta({
        "sources_used": [{"source": "sections/results.md", "role": "direct"}],
        "identity_dimension_values": {"pmid": [111]},
    })
    rows_path, meta_path = builder.write_staging()

    cmd = [
        sys.executable,
        "scripts/extraction/write_extraction.py",
        "iter1",
        "--paper-id",
        "paper_1",
        "--schema",
        str(schema),
        "--bundle",
        str(bundle),
        "--out",
        str(out),
        "--rows-file",
        str(rows_path),
        "--meta-file",
        str(meta_path),
    ]
    proc = subprocess.run(
        cmd,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[write_extraction iter1] wrote 1 rows" in proc.stderr
    assert out.exists()


def test_derived_arithmetic_requires_source_components(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "pmid": paper(111, builder.evidence.metadata("paper_id")),
            "entity_raw": paper("Entity A", quote),
            "outcome_raw": paper("response", quote),
            "comparison_group_raw": paper("group A", quote),
            "n_total": derived(
                10,
                quote,
                derivation="3 + 7 = 10",
                reasoning="Events plus non-events form the group denominator.",
            ),
        })
        failures = chunk.validate()

    assert any(f.code == "missing_source_components" for f in failures)
    rendered = str(failures)
    assert "hint=DERIVED_COMPONENTS" in rendered
    assert "Rows file:" in rendered
    assert "Machine report:" in rendered
    report_path = (
        project
        / "extractions"
        / ".staging"
        / "paper_1"
        / "validation"
        / "results.validate.json"
    )
    report = json.loads(report_path.read_text())
    assert report["groups"][0]["hint_code"] == "DERIVED_COMPONENTS"
    assert report["context"]["rows_path"].endswith("000_results.rows.json")


def test_derived_prose_arithmetic_requires_source_components(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "pmid": paper(111, builder.evidence.metadata("paper_id")),
            "entity_raw": paper("Entity A", quote),
            "outcome_raw": paper("response", quote),
            "comparison_group_raw": paper("group A", quote),
            "n_total": derived(
                10,
                quote,
                derivation="3 events plus 7 non-events gives the group denominator 10.",
                reasoning="Events plus non-events form the group denominator.",
            ),
        })
        failures = chunk.validate()

    assert any(f.code == "missing_source_components" for f in failures)


@pytest.mark.parametrize("reasoning", [
    "Events plus non-events form the group denominator.",
    "For Entity A, group A, the paper reports 3 patients with response and 7 without response. These complementary counts give a group denominator of 10.",
    "The denominator combines the response and non-response counts for group A; it is not an enrollment count for other groups.",
])
def test_derived_source_components_are_validated_and_persisted(tmp_path: Path, reasoning: str) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    out = project / "extractions" / "paper_1_components.jsonl"
    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    event_quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    nonevent_quote = builder.evidence.quote("sections/results.md").exact(
        "The complementary non-event count was 7 patients."
    )
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "pmid": paper(111, builder.evidence.metadata("paper_id")),
            "entity_raw": paper("Entity A", event_quote),
            "outcome_raw": paper("response", event_quote),
            "comparison_group_raw": paper("group A", event_quote),
            "n_total": derived(
                10,
                [event_quote, nonevent_quote],
                derivation="3 + 7 = 10",
                reasoning=reasoning,
                source_components=[
                    source_component("events", 3, event_quote),
                    source_component("non_events", 7, nonevent_quote),
                ],
            ),
        })
        assert chunk.validate() == []
    builder.meta({
        "sources_used": [{"source": "sections/results.md", "role": "direct"}],
        "identity_dimension_values": {"pmid": [111]},
    })

    result = builder.finalize(out)

    assert result.row_count == 1
    row = json.loads(out.read_text().splitlines()[0])
    components = row["provenance"]["n_total"]["source_components"]
    assert [c["label"] for c in components] == ["events", "non_events"]
    assert [c["value"] for c in components] == [3, 7]
    assert row["n_total"] == 10
    assert row["provenance"]["n_total"]["reasoning"] == reasoning
    assert row["provenance"]["n_total"]["derivation"] == "3 + 7 = 10"
    assert all(c["sources"][0]["location"] == "sections/results.md" for c in components)


def test_derived_prose_arithmetic_component_operands_are_validated(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    event_quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    nonevent_quote = builder.evidence.quote("sections/results.md").exact(
        "The complementary non-event count was 7 patients."
    )
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "pmid": paper(111, builder.evidence.metadata("paper_id")),
            "entity_raw": paper("Entity A", event_quote),
            "outcome_raw": paper("response", event_quote),
            "comparison_group_raw": paper("group A", event_quote),
            "n_total": derived(
                10,
                [event_quote, nonevent_quote],
                derivation="3 events plus 7 non-events gives the group denominator 10.",
                reasoning="Events plus non-events form the group denominator.",
                source_components=[
                    source_component("events", 3, event_quote),
                    source_component("non_events", 7, nonevent_quote),
                ],
            ),
        })
        assert chunk.validate() == []


def test_source_component_value_must_appear_in_component_quote(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    event_quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "pmid": paper(111, builder.evidence.metadata("paper_id")),
            "entity_raw": paper("Entity A", event_quote),
            "outcome_raw": paper("response", event_quote),
            "comparison_group_raw": paper("group A", event_quote),
            "n_total": derived(
                10,
                event_quote,
                derivation="3 + 7 = 10",
                reasoning="Events plus non-events form the group denominator.",
                source_components=[
                    source_component("events", 3, event_quote),
                    source_component("non_events", 7, event_quote),
                ],
            ),
        })
        failures = chunk.validate()

    assert any(f.code == "source_component_value_not_in_quote" for f in failures)


def test_arithmetic_operand_must_have_source_component(tmp_path: Path) -> None:
    project, bundle, schema = write_fixture(tmp_path)
    builder = ExtractionBuilder(
        project_dir=project,
        paper_id="paper_1",
        schema_path=schema,
        bundle_dir=bundle,
    )
    event_quote = builder.evidence.quote("sections/results.md").exact(
        "Entity A group A had response in 3 of 10 patients."
    )
    nonevent_quote = builder.evidence.quote("sections/results.md").exact(
        "The complementary non-event count was 7 patients."
    )
    with builder.chunk("results") as chunk:
        chunk.add_row({
            "pmid": paper(111, builder.evidence.metadata("paper_id")),
            "entity_raw": paper("Entity A", event_quote),
            "outcome_raw": paper("response", event_quote),
            "comparison_group_raw": paper("group A", event_quote),
            "n_total": derived(
                10,
                [event_quote, nonevent_quote],
                derivation="3 + 7 = 10",
                reasoning="Events plus non-events form the group denominator.",
                source_components=[
                    source_component("events", 3, event_quote),
                ],
            ),
        })
        failures = chunk.validate()

    assert any(f.code == "derived_operand_missing_component" for f in failures)
