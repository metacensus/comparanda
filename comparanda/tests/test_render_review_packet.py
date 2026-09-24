"""Missing values must not be presented as verbatim scientific claims."""
from pathlib import Path
import json
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.review.render_review_packet import render_packet


def test_packet_distinguishes_missing_values_and_preserves_evidence(tmp_path):
    explanation = "The report says no secondary measurements were collected."
    quote = "No secondary measurements were collected."
    row = {
        "missing_count": None,
        "unclassified_count": None,
        "measured_count": 12,
        "provenance": {
            "missing_count": {
                "missing_reason": "explicitly_unavailable",
                "reasoning": explanation,
                "sources": [{"quote": quote, "location": "sections/results.md", "page": 2}],
            },
            "unclassified_count": {
                "missing_reason": None,
                "reasoning": "No named category fits this source limitation.",
                "sources": [],
            },
            "measured_count": {
                "type": "paper", "derivation_kind": "verbatim", "extraction_certainty": "clean",
                "sources": [{"quote": "Twelve measurements were collected.", "location": "sections/results.md", "page": 2}],
            },
        },
    }
    (tmp_path / "example.jsonl").write_text(json.dumps(row) + "\n")
    packet = render_packet(tmp_path, "example")
    assert "**missing_count — Explicitly not reported / not measured**" in packet
    assert "**unclassified_count — Missing — reason not categorized**" in packet
    assert explanation in packet and quote in packet
    assert "`sections/results.md` p.2" in packet
    assert "**measured_count = 12**  — verbatim, clean" in packet
    assert "= null" not in packet
