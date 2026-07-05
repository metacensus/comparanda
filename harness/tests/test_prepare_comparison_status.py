import json

import pandas as pd

from harness.eval.prepare_comparison import prepare_comparison


def test_prepare_comparison_reports_no_gold_rows_as_precision_only(tmp_path):
    extractions = tmp_path / "extractions"
    extractions.mkdir()
    paper_id = "example_2026_11156223"
    (extractions / f"{paper_id}.jsonl").write_text(
        json.dumps({"entity_raw": "Entity A", "n_total": 10}) + "\n"
    )

    gold = tmp_path / "gold.xlsx"
    pd.DataFrame(
        [
            {
                "PMID": 12345678,
                "Entity": "Other",
                "Patients with Entity": 1,
            }
        ]
    ).to_excel(gold, sheet_name="Individual Outcomes", index=False)

    eval_config = tmp_path / "eval_config"
    eval_config.mkdir()
    (eval_config / "document_id_map.json").write_text("{}")
    (eval_config / "gold_config.json").write_text(
        json.dumps(
            {
                "sheet": "Individual Outcomes",
                "document_id_column": "PMID",
                "entity_columns": ["Entity"],
                "gold_fields": ["Entity", "Patients with Entity"],
                "value_column_map": {"Patients with Entity": "n_total"},
                "extraction_entity_fields": ["entity_raw"],
            }
        )
    )

    result = prepare_comparison(
        paper_id=paper_id,
        extractions_dir=extractions,
        gold_standard_path=gold,
        document_id_map_path=eval_config / "document_id_map.json",
    )

    assert result["document_id"] == 11156223
    assert result["benchmark_status"] == "no_gold_rows"
    assert result["gold_scored"] is False
    assert result["precision_only"] is True
    assert result["gold_rows"] == 0
    assert result["extraction_rows"] == 1
    assert result["unmatched_extraction_count"] == 1
    assert "error" not in result
