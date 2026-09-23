from pathlib import Path
from collections import Counter
from types import SimpleNamespace
import copy
import hashlib
import json

import fitz
import pytest

from scripts.review import portal_export
from scripts.review.portal_export import _prepare_output_dir, main


def test_prepare_output_refuses_to_replace_published_run(tmp_path: Path) -> None:
    out = tmp_path / "runs" / "published"
    out.mkdir(parents=True)
    marker = out / "manifest.json"
    marker.write_text("{}")

    with pytest.raises(FileExistsError, match="output already exists"):
        _prepare_output_dir(out)

    assert marker.read_text() == "{}"


def test_prepare_output_force_is_explicit(tmp_path: Path) -> None:
    out = tmp_path / "candidate"
    out.mkdir()
    (out / "stale.json").write_text("{}")

    _prepare_output_dir(out, force=True)

    assert not (out / "stale.json").exists()
    assert (out / "rows").is_dir()
    assert (out / "bundles").is_dir()
    assert (out / "pdfs").is_dir()


def test_units_only_accepts_an_explicit_snapshot_without_project(tmp_path: Path) -> None:
    missing_snapshot = tmp_path / "missing"

    assert main(["--run-id", "ignored", "--out", str(missing_snapshot), "--units-only"]) == 1


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlink", "missing"])
def test_copy_bundle_rejects_invalid_citations_without_touching_source(tmp_path, kind):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = tmp_path / "outside.md"
    source.write_text("source evidence must remain immutable")
    location = {"absolute": str(source), "traversal": "../outside.md",
                "symlink": "escape.md", "missing": "missing.md"}[kind]
    if kind == "symlink":
        (bundle / location).symlink_to(source)
    dest = tmp_path / "export"
    with pytest.raises((ValueError, FileNotFoundError)):
        portal_export._copy_bundle(bundle, dest, {location})
    assert source.read_text() == "source evidence must remain immutable"
    assert not dest.exists()


def test_component_only_evidence_is_exported_and_anchored_to_its_value(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "sections").mkdir(parents=True)
    for name in ("reported", "operand"):
        (bundle / "sections" / f"{name}.md").write_text(name)
    source = {"quote": "20 total, 4 carriers", "location": "sections/reported.md", "page": 1}
    provenance = {
        "sources": [dict(source)],
        "source_components": [
            {"value": 4, "sources": [dict(source)]},
            {"value": 16, "sources": [{**source, "location": "sections/operand.md"}]},
        ],
    }
    original = copy.deepcopy(provenance)
    def fake_resolve(_bundle, target, stats, field_value=None):
        target["pdf"] = "pdfs/paper.pdf"
        target["resolution"] = "exact"
        target["highlight"] = [{"page": 1, "rects": [{"value": field_value}]}]
        stats["exact"] += 1
    monkeypatch.setattr(portal_export, "_resolve_source_uncached", fake_resolve)
    cited, stats = set(), Counter()
    portal_export.resolve_provenance_sources(
        SimpleNamespace(
            _source_resolution_cache={}, diagnostics=Counter(),
            source_context=lambda src: {
                "key": "primary", "page": src.get("page"),
                "allowed_pages": None,
            },
        ),
        provenance, stats, cited, field_value=20,
    )
    assert stats == {"exact": 1}
    assert provenance["source_components"][0]["sources"][0]["highlight"][0]["rects"] == [{"value": 4}]
    portal_export._copy_bundle(bundle, tmp_path / "export", cited)
    assert (tmp_path / "export/sections/operand.md").read_text() == "operand"
    for pv in [provenance, *provenance["source_components"]]:
        for src in pv["sources"]:
            src.pop("resolution")
            src.pop("highlight")
            src.pop("pdf")
    assert provenance == original


def test_pdf_resolution_cache_matches_uncached_result_without_shared_mutation(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "There were 24 cases among 100 participants.")
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{}")
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    source = {"quote": "There were 24 cases among 100 participants.", "location": "text.md", "page": 1}
    reference, expected_stats = dict(source), Counter()
    portal_export._resolve_source_uncached(bundle, reference, expected_stats, field_value=24)
    first, second, stats = dict(source), dict(source), Counter()
    portal_export.resolve_source(bundle, first, stats, field_value=24)
    portal_export.resolve_source(bundle, second, stats, field_value=24)
    assert first == second == reference
    assert stats == expected_stats + expected_stats
    first["highlight"].clear()
    assert second["highlight"] == reference["highlight"]
    bundle.close()


def test_cached_textpage_prose_search_matches_direct_search_and_is_reused(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "Distinctive prose evidence spans this sentence.")
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{}")
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    query = "Distinctive prose evidence spans this sentence."
    direct = portal_export._search(bundle.doc[0], query)
    cached_page, cached = bundle.prose_search("primary", 0, query, phase="cited_page")
    _, repeated = bundle.prose_search("primary", 0, query, phase="all_page_fallback")

    signature = lambda rects: [tuple(round(value, 4) for value in rect) for rect in rects]
    assert signature(cached) == signature(repeated) == signature(direct)
    assert cached_page.number == 0
    assert bundle.diagnostics["textpage_builds"] == 1
    assert bundle.diagnostics["prose_search_for_calls"] == 1
    assert bundle.diagnostics["prose_search_result_cache_misses"] == 1
    assert bundle.diagnostics["prose_search_result_cache_hits"] == 1
    assert bundle.diagnostics["cited_page_search_for_calls"] == 1
    assert bundle.diagnostics["all_page_fallback_search_for_calls"] == 0
    bundle.close()


def test_same_prose_query_for_different_values_searches_pdf_once(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "The shared evidence supports two derived operands.")
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{}")
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    source = {
        "quote": "The shared evidence supports two derived operands.",
        "location": "sections/evidence.md",
        "page": 1,
    }
    first, second = dict(source), dict(source)
    portal_export.resolve_source(bundle, first, Counter(), field_value=4)
    portal_export.resolve_source(bundle, second, Counter(), field_value=16)

    assert first == second
    assert bundle.diagnostics["source_cache_misses"] == 2
    assert bundle.diagnostics["source_cache_hits"] == 0
    assert bundle.diagnostics["prose_search_result_cache_misses"] == 1
    assert bundle.diagnostics["prose_search_result_cache_hits"] == 1
    assert bundle.diagnostics["prose_search_for_calls"] == 1
    first["highlight"][0]["rects"][0]["x"] = -1
    assert second["highlight"][0]["rects"][0]["x"] >= 0
    bundle.close()


def test_full_page_textpage_never_broadens_clipped_numeric_table_search(tmp_path):
    pdf = tmp_path / "table.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((300, 60), "Count")
        page.insert_text((72, 100), "Cases")
        page.insert_text((300, 100), "11")
        page.insert_text((72, 140), "Controls")
        page.insert_text((300, 140), "11")
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{}")
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)

    # Populate the reusable full-page prose cache before taking the table path.
    cached_page, all_numeric_hits = bundle.prose_search(
        "primary", 0, "11", phase="cited_page",
    )
    assert len(all_numeric_hits) == 2
    table_rect = fitz.Rect(50, 35, 350, 160)
    cell_hits = portal_export._anchor_cell(
        bundle.doc[0], table_rect,
        {"row": "Controls", "column": "Count"}, "11",
        metrics=bundle.diagnostics,
    )
    assert len(cell_hits) == 1
    assert cell_hits[0].y0 > all_numeric_hits[0].y0
    assert bundle.diagnostics["clipped_search_for_calls"] >= 3

    textpage = bundle._full_page_textpages[("primary", 0)][1]
    with pytest.raises(ValueError, match="full-page TextPage"):
        portal_export._search(cached_page, "11", clip=table_rect, textpage=textpage)
    bundle.close()


def test_clipped_search_cache_uses_exact_clip_and_returns_fresh_rects(tmp_path):
    pdf = tmp_path / "table-cache.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((300, 60), "Count")
        page.insert_text((72, 100), "Cases")
        page.insert_text((300, 100), "11")
        page.insert_text((72, 140), "Controls")
        page.insert_text((300, 140), "11")
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{}")
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    page = bundle.doc[0]
    upper_clip = fitz.Rect(280, 80, 330, 110)
    lower_clip = fitz.Rect(280, 120, 330, 150)

    lower_first = bundle.clipped_search("primary", page, "11", lower_clip)
    lower_second = bundle.clipped_search("primary", page, "11", lower_clip)
    upper = bundle.clipped_search("primary", page, "11", upper_clip)
    assert len(lower_first) == len(lower_second) == len(upper) == 1
    assert lower_first[0].y0 == lower_second[0].y0 > upper[0].y0
    assert bundle.diagnostics["clipped_search_result_cache_hits"] == 1
    assert bundle.diagnostics["clipped_search_result_cache_misses"] == 2
    lower_first[0].x0 = -1
    assert lower_second[0].x0 >= 0

    table_rect = fitz.Rect(50, 35, 350, 160)
    search = lambda p, q, c: bundle.clipped_search("primary", p, q, c)
    controls = portal_export._anchor_cell(
        page, table_rect, {"row": "Controls", "column": "Count"}, "11",
        clipped_search=search,
    )
    controls_again = portal_export._anchor_cell(
        page, table_rect, {"row": "Controls", "column": "Count"}, "11",
        clipped_search=search,
    )
    cases = portal_export._anchor_cell(
        page, table_rect, {"row": "Cases", "column": "Count"}, "11",
        clipped_search=search,
    )
    assert len(controls) == len(controls_again) == len(cases) == 1
    assert controls[0].y0 == controls_again[0].y0 > cases[0].y0
    assert bundle.diagnostics["clipped_search_result_cache_hits"] >= 4
    bundle.close()


def test_rotated_page_prose_and_table_rects_use_display_coordinates(tmp_path):
    pdf = tmp_path / "rotated.pdf"
    table_unrotated = fitz.Rect(20, 70, 160, 250)
    prose = "Rotated prose bottom"
    with fitz.open() as doc:
        page = doc.new_page(width=200, height=300)
        page.insert_text((30, 260), prose)
        # Raw coordinates are arranged so the declared 90° page rotation makes
        # Cases/Controls display as rows and Count/11 display as one column.
        page.insert_text((30, 100), "Count", rotate=90)
        page.insert_text((80, 200), "Cases", rotate=90)
        page.insert_text((80, 100), "11", rotate=90)
        page.insert_text((130, 200), "Controls", rotate=90)
        page.insert_text((130, 100), "11", rotate=90)
        page.set_rotation(90)
        doc.save(pdf)

    with fitz.open(pdf) as doc:
        page = doc[0]
        displayed_table = table_unrotated * page.rotation_matrix
        display_height = page.rect.height
        docling_bbox = [
            displayed_table.x0,
            display_height - displayed_table.y0,
            displayed_table.x1,
            display_height - displayed_table.y1,
        ]

    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "_provenance").mkdir(parents=True)
    (bundle_dir / "manifest.json").write_text(json.dumps({"tables": [{
        "path": "tables/table_1.json", "docling_idx": 0,
    }]}))
    (bundle_dir / "_provenance/docling_tables.json").write_text(json.dumps({
        "tables": [{"index": 0, "page": 1, "bbox": docling_bbox}],
    }))
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    page = bundle.doc[0]
    assert page.rotation == 90
    table_context = bundle.source_context({
        "location": "tables/table_1.json", "page": 1,
    })
    assert tuple(bundle.table_rect("tables/table_1.json", table_context)[1]) == pytest.approx(
        tuple(table_unrotated)
    )

    prose_source = {"quote": prose, "location": "sections/text.md", "page": 1}
    portal_export._resolve_source_uncached(bundle, prose_source, Counter())
    prose_hits = portal_export._search(page, portal_export.normalize_quote(prose))
    expected_prose = portal_export._norm_rects(prose_hits, page)
    assert prose_source["resolution"] == "exact"
    assert prose_source["highlight"][0]["rects"] == expected_prose
    assert prose_hits[0].y0 > page.rect.height  # would fail under old normalization
    assert all(
        0 <= rect[key] <= 1
        for rect in expected_prose for key in ("x", "y", "w", "h")
    )

    table_source = {
        "quote": "11", "location": "tables/table_1.json", "page": 1,
        "cell": {"row": "Controls", "column": "Count"},
    }
    portal_export._resolve_source_uncached(
        bundle, table_source, Counter(), field_value=11,
    )
    anchored = portal_export._anchor_cell(
        page, table_unrotated,
        {"row": "Controls", "column": "Count"}, "11",
    )
    assert table_source["resolution"] == "exact"
    assert len(anchored) == 1
    assert table_source["highlight"][0]["rects"] == portal_export._norm_rects(
        anchored, page,
    )
    assert all(
        0 <= rect[key] <= 1
        for rect in table_source["highlight"][0]["rects"]
        for key in ("x", "y", "w", "h")
    )
    bundle.close()


def test_identical_component_sources_with_different_operands_do_not_share_cache(monkeypatch):
    calls = []

    def fake_resolve(_bundle, target, stats, field_value=None):
        calls.append(field_value)
        target["pdf"] = "pdfs/paper.pdf"
        target["resolution"] = "exact"
        target["highlight"] = [{"page": 1, "rects": [{"value": field_value}]}]
        stats["exact"] += 1

    monkeypatch.setattr(portal_export, "_resolve_source_uncached", fake_resolve)
    source = {"quote": "Shared operand evidence", "location": "operand.md", "page": 1}
    provenance = {"source_components": [
        {"value": 4, "sources": [dict(source)]},
        {"value": 16, "sources": [dict(source)]},
        {"value": 4, "sources": [dict(source)]},
    ]}
    bundle = SimpleNamespace(
        _source_resolution_cache={}, diagnostics=Counter(),
        source_context=lambda src: {
            "key": "primary", "page": src.get("page"), "allowed_pages": None,
        },
    )
    portal_export.resolve_provenance_sources(
        bundle, provenance, Counter(), set(), field_value=20,
    )

    assert calls == [4, 16]
    highlights = [
        component["sources"][0]["highlight"][0]["rects"][0]["value"]
        for component in provenance["source_components"]
    ]
    assert highlights == [4, 16, 4]
    assert bundle.diagnostics["source_cache_misses"] == 2
    assert bundle.diagnostics["source_cache_hits"] == 1
    assert bundle.diagnostics["resolution_exact"] == 3


def test_supplement_table_routes_pdf_and_maps_virtual_page_without_rewriting_source(tmp_path):
    project = tmp_path / "project"
    bundle_dir = project / "data/processed/paper"
    supplement = project / "data/raw/supplements/paper/supplement.pdf"
    primary = project / "data/raw/pdfs/paper.pdf"
    supplement.parent.mkdir(parents=True)
    primary.parent.mkdir(parents=True)
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), "Primary article text")
        doc.save(primary)
    with fitz.open() as doc:
        for page_no in range(1, 7):
            page = doc.new_page()
            if page_no == 3:
                page.insert_text((72, 72), "Distinctive supplementary evidence")
                page.insert_text((72, 100), "11 11 11")
        doc.save(supplement)
    supplement_sha = hashlib.sha256(supplement.read_bytes()).hexdigest()
    table = {
        "path": "tables/table_S3.json", "markdown_path": "tables/table_S3.md",
        "page": 13, "page_range": [13, 16], "supplement_page_range": [3, 6],
        "source_artifact": "historical/path/supplement.pdf",
        "source_sha256": supplement_sha,
    }
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_text(json.dumps({"tables": [table]}))
    bundle = portal_export.Bundle("paper", bundle_dir, primary)
    bundle.prepare_documents()

    source = {
        "quote": "Distinctive supplementary evidence",
        "location": "tables/table_S3.md", "page": 13,
    }
    original = copy.deepcopy(source)
    portal_export._resolve_source_uncached(bundle, source, Counter())
    assert {k: source[k] for k in original} == original
    assert source["pdf"] == f"pdfs/paper/supplement-{supplement_sha[:12]}.pdf"
    assert source["resolution"] == "exact"
    assert source["highlight"][0]["page"] == 3

    numeric = {
        "quote": "11", "location": "tables/table_S3.json", "page": 13,
    }
    portal_export._resolve_source_uncached(bundle, numeric, Counter(), field_value=11)
    assert numeric["resolution"] == "page_only"
    assert numeric["highlight"] == [{"page": 3, "rects": []}]
    assert numeric["page"] == 13
    assert numeric["pdf"] == source["pdf"]

    primary_source = {"quote": "Primary article text", "location": "text.md", "page": 1}
    portal_export._resolve_source_uncached(bundle, primary_source, Counter())
    assert primary_source["pdf"] == "pdfs/paper.pdf"
    catalog = bundle.document_catalog()
    assert {(entry["role"], entry["pdf"]) for entry in catalog} == {
        ("primary", "pdfs/paper.pdf"),
        ("supplement", source["pdf"]),
    }
    assert next(e for e in catalog if e["role"] == "supplement") == {
        "pdf": source["pdf"], "sha256": supplement_sha,
        "page_count": 6, "role": "supplement",
    }
    exported = tmp_path / "export"
    bundle.copy_documents(exported)
    assert (exported / source["pdf"]).read_bytes() == supplement.read_bytes()
    assert (exported / "pdfs/paper.pdf").read_bytes() == primary.read_bytes()
    bundle.close()


def test_supplement_hash_mismatch_fails_instead_of_searching_primary(tmp_path):
    project = tmp_path / "project"
    bundle_dir = project / "data/processed/paper"
    supplement = project / "data/raw/supplements/paper/supplement.pdf"
    supplement.parent.mkdir(parents=True)
    with fitz.open() as doc:
        doc.new_page()
        doc.save(supplement)
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.json").write_text(json.dumps({"tables": [{
        "path": "tables/table.json", "page": 1, "page_range": [1, 1],
        "source_artifact": "data/raw/supplements/paper/supplement.pdf",
        "source_sha256": "0" * 64,
    }]}))
    bundle = portal_export.Bundle("paper", bundle_dir, project / "data/raw/pdfs/paper.pdf")
    with pytest.raises(FileNotFoundError, match="supplement PDF with sha256"):
        bundle.prepare_documents()


def test_full_quote_is_found_on_section_page_before_a_prefix_elsewhere(tmp_path):
    pdf = tmp_path / "paper.pdf"
    quote = "Distinctive section evidence supports the reported outcome."
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), quote[:40])
        doc.new_page().insert_text((72, 72), quote)
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text(json.dumps({"sections": [{
        "path": "sections/results.md", "page_range": [2, 2],
    }]}))
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    source = {"quote": quote, "location": "sections/results.md", "page": 1}
    portal_export._resolve_source_uncached(bundle, source, Counter())
    assert source["resolution"] == "exact"
    assert source["highlight"][0]["page"] == 2
    bundle.close()


def test_word_fallback_handles_pdf_line_hyphenation_without_numeric_fuzzing(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "Distinctive evi-")
        page.insert_text((72, 88), "dence supports outcome 0.1.")
        doc.save(pdf)
    with fitz.open(pdf) as doc:
        page = doc[0]
        assert portal_export._full_quote_word_match(
            page, "Distinctive evidence supports outcome 0.1.",
        )
        assert not portal_export._full_quote_word_match(
            page, "Distinctive evidence supports outcome 01.",
        )


def test_compact_quote_fallback_recovers_lost_space_without_numeric_fuzzing(tmp_path):
    pdf = tmp_path / "compact.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "Distinctive nZ 834,38% received outcome 0.1.")
        doc.save(pdf)
    with fitz.open(pdf) as doc:
        page = doc[0]
        assert portal_export._full_quote_compact_match(
            page, "Distinctive n Z 834,38% received outcome 0.1.",
        )
        assert not portal_export._full_quote_compact_match(
            page, "Distinctive n Z 834,38% received outcome 01.",
        )


def test_compact_quote_fallback_rejects_repeated_quote(tmp_path):
    pdf = tmp_path / "repeated.pdf"
    quote = "Distinctive nZ received the reported outcome."
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), quote)
        page.insert_text((72, 100), quote)
        doc.save(pdf)
    with fitz.open(pdf) as doc:
        assert not portal_export._full_quote_compact_match(
            doc[0], "Distinctive n Z received the reported outcome.",
        )


def test_character_quote_fallback_recovers_spaces_lost_inside_pdf_word(tmp_path):
    pdf = tmp_path / "characters.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "The trialassigned410patientswith consent.")
        doc.save(pdf)
    with fitz.open(pdf) as doc:
        page = doc[0]
        assert portal_export._full_quote_character_match(
            page, "trial assigned 410 patients with",
        )
        assert not portal_export._full_quote_character_match(
            page, "trial assigned 0410 patients with",
        )


def test_character_quote_normalizes_markdown_asterisks_per_pdf_glyph(tmp_path):
    pdf = tmp_path / "glyph-normalization.pdf"
    text = "Distinctive DPYD*2A finding supports participants."
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        doc.save(pdf)
    with fitz.open(pdf) as doc:
        assert portal_export._full_quote_character_match(
            doc[0], "Distinctive **DPYD*2A** finding supports participants.",
        )


def test_character_rect_merge_contains_every_input_glyph():
    glyphs = [fitz.Rect(10, 20, 14, 30), fitz.Rect(14, 20, 18, 30), fitz.Rect(18, 20, 22, 30)]
    merged = portal_export._merge_character_rects(glyphs)
    assert len(merged) == 1
    assert all(merged[0].contains(glyph) for glyph in glyphs)


def test_figure_caption_full_quote_stays_exact_before_figure_region(tmp_path):
    pdf = tmp_path / "figure-caption.pdf"
    quote = "Figure 1. Multivariable logistic regression supports the outcome."
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), quote)
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "_provenance").mkdir(parents=True)
    (bundle_dir / "manifest.json").write_text(json.dumps({"figures": [{
        "path": "figures/figure_0.png", "docling_idx": 1,
    }]}))
    (bundle_dir / "_provenance/docling_pictures.json").write_text(json.dumps({
        "pictures": [{"index": 1, "page": 1, "bbox": [20, 250, 180, 100]}],
    }))
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    source = {"quote": quote, "location": "figures/figure_0.png", "page": 1}
    portal_export._resolve_source_uncached(bundle, source, Counter())
    assert source["resolution"] == "exact"
    bundle.close()


def test_figure_location_uses_preserved_picture_region(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        doc.new_page(width=200, height=300)
        doc.save(pdf)
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "_provenance").mkdir(parents=True)
    (bundle_dir / "manifest.json").write_text(json.dumps({"figures": [{
        "path": "figures/figure_0.png", "docling_idx": 4,
    }]}))
    (bundle_dir / "_provenance/docling_pictures.json").write_text(json.dumps({
        "pictures": [{"index": 4, "page": 1, "bbox": [20, 250, 180, 100]}],
    }))
    bundle = portal_export.Bundle("paper", bundle_dir, pdf)
    source = {"quote": "non-text figure read", "location": "figures/figure_0.png", "page": 1}
    portal_export._resolve_source_uncached(bundle, source, Counter())
    assert source["resolution"] == "figure_region"
    rect = source["highlight"][0]["rects"][0]
    assert rect == pytest.approx({"x": .1, "y": 1 / 6, "w": .8, "h": .5})
    bundle.close()


def test_full_export_preserves_rows_and_component_citations(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    bundle = project / "data/processed/paper"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{}")
    (bundle / "operand.md").write_text("4 carriers")
    (project / "extractions").mkdir()
    (project / "review_queue.json").write_text('{"items": []}')
    row = {
        "document_id": "paper", "n": 4,
        "provenance": {"n": {"extraction_certainty": "clean", "source_components": [
            {"value": 4, "sources": [{"location": "operand.md", "quote": "4 carriers", "page": 1}]}
        ]}},
    }
    source_file = project / "extractions/paper.jsonl"
    source_file.write_text(json.dumps(row) + '\n' + json.dumps({"_meta": {"schema_version": "v006"}}) + '\n')
    original_bytes = source_file.read_bytes()
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out), "--blind"]) == 0
    raw = json.loads((out / "rows/paper.json").read_text())
    # raw rows hold integer references; expansion rebuilds the v001 shape
    assert raw[0]["provenance"]["n"]["source_components"][0]["sources"] == [0]
    exported = portal_export.load_snapshot_rows(out, "paper")
    assert len(exported) == 1
    assert exported[0]["fields"] == {k: v for k, v in row.items() if k != "provenance"}
    component_source = exported[0]["provenance"]["n"]["source_components"][0]["sources"][0]
    assert component_source["resolution"] == "page_only"
    assert component_source["pdf"] == "pdfs/paper.pdf"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["snapshot_format_version"] == "v002"
    assert manifest["papers"][0]["documents"] == [{
        "pdf": "pdfs/paper.pdf", "sha256": None,
        "page_count": 0, "role": "primary",
    }]
    assert (out / "bundles/paper/operand.md").read_text() == "4 carriers"
    assert source_file.read_bytes() == original_bytes


def _mini_project(tmp_path: Path, row: dict, quote_text: str = "4 carriers"):
    """Build a one-paper project exporting without a PDF (page_only highlights)."""
    project = tmp_path / "project"
    bundle = project / "data/processed/paper"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{}")
    (bundle / "operand.md").write_text(quote_text)
    (project / "extractions").mkdir()
    (project / "review_queue.json").write_text('{"items": []}')
    (project / "extractions/paper.jsonl").write_text(json.dumps(row) + "\n")
    return project


_MINI_ROW = {
    "document_id": "paper", "n": 4,
    "provenance": {
        "n": {
            "reasoning": "why four",
            "derivation": "how four",
            "sources": [{"location": "operand.md", "quote": "4 carriers", "page": 1}],
            "source_components": [
                {"value": 4, "sources": [{"location": "operand.md", "quote": "4 carriers", "page": 1}]},
                {"value": 4, "sources": [{"location": "operand.md", "quote": "4 carriers", "page": 1}]},
            ],
        },
        "m": {
            "reasoning": "why four",
            "sources": [{"location": "operand.md", "quote": "missing", "page": 1}],
        },
    },
}


def test_export_writes_v002_rows_and_evidence(tmp_path: Path) -> None:
    project = _mini_project(tmp_path, _MINI_ROW)
    out = tmp_path / "out"
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out), "--blind"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["snapshot_format_version"] == "v002"
    ev = json.loads((out / "evidence/paper.json").read_text())
    assert ev["snapshot_format_version"] == "v002"
    assert ev["paper_id"] == "paper"
    assert len(ev["sources"]) == 2  # "4 carriers" + "missing", distinct per paper
    assert ev["reasoning"] == ["why four"]
    assert ev["derivation"] == ["how four"]
    # rows and evidence are compact JSON (no indent)
    assert "\n" not in (out / "rows/paper.json").read_text().strip("\n")
    expanded = portal_export.load_snapshot_rows(out, "paper")
    assert expanded[0]["provenance"]["n"]["sources"] == [
        {"location": "operand.md", "quote": "4 carriers", "page": 1,
         "pdf": "pdfs/paper.pdf", "resolution": "page_only",
         "highlight": [{"page": 1, "rects": []}]}
    ]
    assert expanded[0]["provenance"]["n"]["reasoning"] == "why four"


def test_distinct_source_resolved_once_per_paper(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_resolve(_bundle, target, stats, field_value=None):
        calls.append((target.get("quote"), field_value))
        target["pdf"] = "pdfs/paper.pdf"
        target["resolution"] = "page_only"
        target["highlight"] = [{"page": 1, "rects": []}]
        stats["page_only"] += 1

    monkeypatch.setattr(portal_export, "_resolve_source_uncached", fake_resolve)
    project = _mini_project(tmp_path, _MINI_ROW)
    out = tmp_path / "out"
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out), "--blind"]) == 0
    # "4 carriers" (value 4) resolved once despite 3 occurrences; "missing" once
    assert calls == [("4 carriers", 4), ("missing", None)]


def _tampered_cache_export(tmp_path: Path, monkeypatch) -> Path:
    """First export becomes the cache; tamper one highlight + resolution in it."""
    monkeypatch.setattr(
        portal_export, "_resolve_source_uncached",
        lambda _b, target, stats, field_value=None: (
            target.update({"pdf": "pdfs/paper.pdf", "resolution": "exact",
                           "highlight": [{"page": 7, "rects": []}]}),
            stats.update({"exact": 1}),
        ),
    )
    project = _mini_project(tmp_path, _MINI_ROW)
    cache = tmp_path / "cache"
    assert main(["--project", str(project), "--run-id", "test",
                 "--out", str(cache), "--blind"]) == 0
    return cache


def test_highlight_cache_reuses_published_highlights(tmp_path: Path, monkeypatch) -> None:
    cache = _tampered_cache_export(tmp_path, monkeypatch)

    def _boom(_bundle, _target, _stats, field_value=None):
        raise AssertionError("seeded source must not be recomputed")

    monkeypatch.setattr(portal_export, "_resolve_source_uncached", _boom)
    project = tmp_path / "project"
    out = tmp_path / "out"
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out),
                 "--blind", "--highlight-cache", str(cache)]) == 0
    expanded = portal_export.load_snapshot_rows(out, "paper")
    pv = expanded[0]["provenance"]["n"]
    assert pv["sources"][0]["resolution"] == "exact"
    assert pv["sources"][0]["highlight"] == [{"page": 7, "rects": []}]
    manifest = json.loads((out / "manifest.json").read_text())
    diag = manifest["papers"][0]["highlight_diagnostics"]
    assert diag["highlight_cache_status"] == "seeded"
    assert diag["highlight_cache_misses"] == 0
    assert diag["source_cache_misses"] == 0


def test_highlight_cache_accepts_v001_snapshot(tmp_path: Path, monkeypatch) -> None:
    cache = _tampered_cache_export(tmp_path, monkeypatch)
    # downgrade the cache export to v001 shape (expanded rows, no evidence/marker)
    expanded = portal_export.load_snapshot_rows(cache, "paper")
    (cache / "rows/paper.json").write_text(json.dumps(expanded))
    manifest = json.loads((cache / "manifest.json").read_text())
    del manifest["snapshot_format_version"]
    (cache / "manifest.json").write_text(json.dumps(manifest))

    def _boom(_bundle, _target, _stats, field_value=None):
        raise AssertionError("seeded source must not be recomputed")

    monkeypatch.setattr(portal_export, "_resolve_source_uncached", _boom)
    out = tmp_path / "out"
    assert main(["--project", str(tmp_path / "project"), "--run-id", "test",
                 "--out", str(out), "--blind", "--highlight-cache", str(cache)]) == 0
    diag = json.loads((out / "manifest.json").read_text())["papers"][0]["highlight_diagnostics"]
    assert diag["highlight_cache_status"] == "seeded"
    assert diag["highlight_cache_misses"] == 0


def test_highlight_cache_documents_mismatch_recomputes(tmp_path: Path, monkeypatch) -> None:
    cache = _tampered_cache_export(tmp_path, monkeypatch)
    # tamper the cache's published documents so the guards fail
    manifest = json.loads((cache / "manifest.json").read_text())
    manifest["papers"][0]["documents"].append(
        {"pdf": "pdfs/extra.pdf", "sha256": "0" * 64, "page_count": 0, "role": "primary"})
    (cache / "manifest.json").write_text(json.dumps(manifest))
    calls = []
    monkeypatch.setattr(
        portal_export, "_resolve_source_uncached",
        lambda _b, t, s, field_value=None: (
            calls.append(1),
            t.update({"pdf": "pdfs/paper.pdf", "resolution": "page_only",
                      "highlight": [{"page": 1, "rects": []}]}),
            s.update({"page_only": 1}),
        ),
    )
    out = tmp_path / "out"
    assert main(["--project", str(tmp_path / "project"), "--run-id", "test",
                 "--out", str(out), "--blind", "--highlight-cache", str(cache)]) == 0
    assert calls  # resolved fresh, not from cache
    diag = json.loads((out / "manifest.json").read_text())["papers"][0]["highlight_diagnostics"]
    assert diag["highlight_cache_status"] == "documents_mismatch"
    assert diag["highlight_cache_misses"] == 0


def test_highlight_cache_conflict_is_dropped(tmp_path: Path) -> None:
    project = _mini_project(tmp_path, _MINI_ROW)
    bundle = portal_export.Bundle(
        "paper", project / "data/processed/paper",
        project / "data/raw/pdfs/paper.pdf")
    bundle.prepare_documents()
    cache = tmp_path / "cache"
    (cache / "rows").mkdir(parents=True)
    (cache / "bundles/paper").mkdir(parents=True)
    # three occurrences of one key with results r1, r2, r1: the key stays
    # dropped after the conflict and is never seeded
    first = {"quote": "4 carriers", "location": "operand.md", "page": 1,
             "pdf": "pdfs/paper.pdf", "resolution": "exact",
             "highlight": [{"page": 1, "rects": []}]}
    second = dict(first, resolution="page_only")
    made = [
        {"row_index": 0, "identity": {}, "fields": {"n": 4},
         "provenance": {"n": {"sources": [first]}}},
        {"row_index": 1, "identity": {}, "fields": {"n": 4},
         "provenance": {"n": {"sources": [second]}}},
        {"row_index": 2, "identity": {}, "fields": {"n": 4},
         "provenance": {"n": {"sources": [dict(first)]}}},
    ]
    (cache / "rows/paper.json").write_text(json.dumps(made))
    (cache / "bundles/paper/manifest.json").write_text(
        (project / "data/processed/paper/manifest.json").read_text())
    docs = bundle.document_catalog()
    bundle.close()
    (cache / "manifest.json").write_text(json.dumps(
        {"papers": [{"paper_id": "paper", "documents": docs}]}))
    fresh = portal_export.Bundle("paper", project / "data/processed/paper",
                                 project / "data/raw/pdfs/paper.pdf")
    fresh.prepare_documents()
    report = portal_export.seed_highlight_cache(fresh, cache, "paper")
    assert report["status"] == "seeded"
    assert report["conflicts"] == 1
    assert report["seeded_keys"] == 0
    assert fresh._source_resolution_cache == {}
    fresh.close()


def test_highlight_cache_refuses_output_dir(tmp_path: Path) -> None:
    project = _mini_project(tmp_path, _MINI_ROW)
    out = tmp_path / "out"
    out.mkdir()
    marker = out / "marker.txt"
    marker.write_text("untouched")
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out),
                 "--blind", "--highlight-cache", str(out), "--force"]) == 2
    # nothing was written: no rows produced, stale marker intact
    assert not (out / "rows").exists()
    assert marker.read_text() == "untouched"
    # cache nested inside out is also refused, before --force deletes anything
    nested = out / "cache"
    nested.mkdir()
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out),
                 "--blind", "--highlight-cache", str(nested), "--force"]) == 2
    assert not (out / "rows").exists()
    assert marker.read_text() == "untouched"


def test_highlight_cache_bundle_manifest_mismatch_recomputes(
    tmp_path: Path, monkeypatch,
) -> None:
    cache = _tampered_cache_export(tmp_path, monkeypatch)
    # change the cache's bundle manifest bytes so the guards fail
    (cache / "bundles/paper/manifest.json").write_text('{"tables": []}')
    calls = []
    monkeypatch.setattr(
        portal_export, "_resolve_source_uncached",
        lambda _b, t, s, field_value=None: (
            calls.append(1),
            t.update({"pdf": "pdfs/paper.pdf", "resolution": "page_only",
                      "highlight": [{"page": 1, "rects": []}]}),
            s.update({"page_only": 1}),
        ),
    )
    out = tmp_path / "out"
    assert main(["--project", str(tmp_path / "project"), "--run-id", "test",
                 "--out", str(out), "--blind", "--highlight-cache", str(cache)]) == 0
    assert calls  # resolved fresh, not from cache
    diag = json.loads((out / "manifest.json").read_text())["papers"][0]["highlight_diagnostics"]
    assert diag["highlight_cache_status"] == "bundle_manifest_mismatch"
    assert diag["highlight_cache_misses"] == 0


def test_highlight_stats_has_canonical_resolution_keys(tmp_path: Path) -> None:
    project = _mini_project(tmp_path, _MINI_ROW)
    out = tmp_path / "out"
    assert main(["--project", str(project), "--run-id", "test", "--out", str(out), "--blind"]) == 0
    stats = json.loads((out / "manifest.json").read_text())["papers"][0]["highlight_stats"]
    # top-level occurrences only: n.sources (1) + m.sources (1); component
    # sources count into a separate Counter. Extra counters (pdf_missing)
    # are allowed on uncached runs, as today.
    for key, want in (("exact", 0), ("partial_quote", 0), ("table_region", 0),
                      ("figure_region", 0), ("page_only", 2)):
        assert stats[key] == want


def test_load_snapshot_rows_expands_and_validates(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    (snap / "rows").mkdir(parents=True)
    (snap / "evidence").mkdir()
    src = {"quote": "q", "location": "a.md", "page": 1, "pdf": "pdfs/p.pdf",
           "resolution": "exact", "highlight": [{"page": 1, "rects": []}]}
    rows = [{"row_index": 0, "identity": {}, "fields": {},
             "provenance": {"f": {
                 "sources": [0, 0],
                 "source_components": [{"value": 1, "sources": [0]}],
                 "reasoning": 0, "derivation": 0,
                 "type": "paper"}}}]
    ev = {"snapshot_format_version": "v002", "paper_id": "p",
          "sources": [src], "reasoning": ["r"], "derivation": ["d"]}
    (snap / "rows/p.json").write_text(json.dumps(rows))
    (snap / "evidence/p.json").write_text(json.dumps(ev))
    v001 = [{"row_index": 0, "identity": {}, "fields": {},
             "provenance": {"f": {
                 "sources": [src, src],
                 "source_components": [{"value": 1, "sources": [src]}],
                 "reasoning": "r", "derivation": "d", "type": "paper"}}}]
    (snap / "manifest.json").write_text(json.dumps({"snapshot_format_version": "v002"}))
    expanded = portal_export.load_snapshot_rows(snap, "p")
    assert json.loads(json.dumps(expanded, sort_keys=True)) == \
        json.loads(json.dumps(v001, sort_keys=True))
    # v001 snapshot passes through (v001 rows on disk, no marker)
    (snap / "rows/p.json").write_text(json.dumps(v001))
    (snap / "manifest.json").write_text(json.dumps({}))
    assert portal_export.load_snapshot_rows(snap, "p") == v001
    # bad ref, double expansion, unknown version all raise
    (snap / "manifest.json").write_text(json.dumps({"snapshot_format_version": "v002"}))
    (snap / "rows/p.json").write_text(json.dumps(v001))  # resolved objects, not ints
    with pytest.raises(ValueError):
        portal_export.load_snapshot_rows(snap, "p")
    (snap / "rows/p.json").write_text(json.dumps(
        [{"row_index": 0, "identity": {}, "fields": {},
          "provenance": {"f": {"sources": [5]}}}]))
    with pytest.raises(ValueError):
        portal_export.load_snapshot_rows(snap, "p")
    (snap / "manifest.json").write_text(json.dumps({"snapshot_format_version": "v009"}))
    with pytest.raises(ValueError):
        portal_export.load_snapshot_rows(snap, "p")


def test_intern_evidence_scan_order_and_repeats(tmp_path: Path) -> None:
    def _src(q):
        return {"quote": q, "location": "a.md", "page": 1}
    rows = [{"row_index": 0, "identity": {}, "fields": {},
             "provenance": {
                 "b": {"sources": [_src("two")], "reasoning": "shared"},
                 "a": {"sources": [_src("one"), _src("one")],
                       "source_components": [{"value": 1, "sources": [_src("two")]}],
                       "derivation": "shared"},
             }}]
    ev = portal_export.intern_paper_evidence(rows)
    # file order: b first (two), then a (one, already seen two); probe/compare
    # use insertion order for the provenance scan
    assert ev["sources"] == [_src("two"), _src("one")]
    assert ev["reasoning"] == ["shared"]
    assert ev["derivation"] == ["shared"]
    assert rows[0]["provenance"]["a"]["sources"] == [1, 1]  # repeats keep order
    assert rows[0]["provenance"]["a"]["source_components"][0]["sources"] == [0]
    assert rows[0]["provenance"]["b"]["sources"] == [0]
    expanded = copy.deepcopy(rows)
    snap = tmp_path / "snap"
    (snap / "rows").mkdir(parents=True)
    (snap / "evidence").mkdir()
    (snap / "rows/p.json").write_text(json.dumps(expanded))
    (snap / "evidence/p.json").write_text(json.dumps(
        {"snapshot_format_version": "v002", "paper_id": "p",
         "sources": ev["sources"], "reasoning": ev["reasoning"],
         "derivation": ev["derivation"]}))
    (snap / "manifest.json").write_text(json.dumps({"snapshot_format_version": "v002"}))
    assert portal_export.load_snapshot_rows(snap, "p")[0]["provenance"]["a"]["sources"] == \
        [_src("one"), _src("one")]
