from harness.eval.semantic_value_gate import compute_semantic_value_gate


def test_semantic_value_gate_accepts_complement_transform():
    comparisons = {
        "paper_a": {
            "paper_id": "paper_a",
            "benchmark_status": "gold_scored",
            "gold_scored": True,
            "gold_rows": 1,
            "extraction_rows": 1,
            "paired_rows": [
                {
                    "gold_index": 0,
                    "gold_fields": {"Patients with Variant and Outcome": 44},
                    "extraction_candidates": [
                        {
                            "extraction_index": 7,
                            "n_total": 336,
                            "n_events": 292,
                        }
                    ],
                    "has_match": True,
                }
            ],
            "unmatched_extraction_rows": [],
        }
    }
    finding_scores = {
        "papers": {
            "paper_a": {
                "findings": [
                    {
                        "gold_index": 0,
                        "completeness": 1.0,
                        "accuracy": 1.0,
                        "extraction_index": 7,
                        "reasoning": "Low-grade count is the complement.",
                        "human_rationale": "Low-grade count is n_total minus severe events.",
                        "match": {
                            "status": "represented",
                            "primary_extraction_index": 7,
                            "matched_extraction_indices": [7],
                            "candidate_pool_size": 1,
                            "identity_basis": [],
                        },
                        "field_adjudications": [
                            {
                                "gold_field": "Patients with Variant and Outcome",
                                "status": "represented_by_transform",
                                "gold_value": 44,
                                "extraction_value": 44,
                                "extraction_expression": {
                                    "op": "complement",
                                    "row": 7,
                                    "total_field": "n_total",
                                    "event_field": "n_events",
                                },
                                "explanation": "336 total minus 292 severe events equals 44.",
                            }
                        ],
                    }
                ]
            }
        }
    }

    report = compute_semantic_value_gate(
        finding_scores=finding_scores,
        comparisons=comparisons,
        require_structured=True,
    )

    assert report["value_gate_passed"] is True
    assert report["mismatch_count"] == 0
    assert report["transform_count"] == 1
    assert report["all_findings_structured"] is True


def test_semantic_value_gate_rejects_legacy_records_when_structured_required():
    comparisons = {
        "paper_a": {
            "paper_id": "paper_a",
            "benchmark_status": "gold_scored",
            "gold_scored": True,
            "gold_rows": 1,
            "paired_rows": [{"gold_index": 0, "gold_fields": {"N": 1}}],
        }
    }
    finding_scores = {
        "papers": {
            "paper_a": {
                "findings": [
                    {
                        "gold_index": 0,
                        "completeness": 1.0,
                        "accuracy": 1.0,
                        "extraction_index": None,
                        "reasoning": "legacy only",
                    }
                ]
            }
        }
    }

    report = compute_semantic_value_gate(
        finding_scores=finding_scores,
        comparisons=comparisons,
        require_structured=True,
    )

    assert report["value_gate_passed"] is False
    assert "structured_adjudications_incomplete" in report["gate_failures"]


def test_semantic_value_gate_accepts_sum_rows_field_shorthand_and_non_numeric_direct():
    comparisons = {
        "paper_a": {
            "paper_id": "paper_a",
            "benchmark_status": "gold_scored",
            "gold_scored": True,
            "gold_rows": 1,
            "paired_rows": [
                {
                    "gold_index": 0,
                    "gold_fields": {"Patients with Outcome": 4},
                    "extraction_candidates": [
                        {"extraction_index": 0, "n_events": 1, "effect_size_type": "not_reported"},
                        {"extraction_index": 1, "n_events": 3, "effect_size_type": "not_reported"},
                    ],
                }
            ],
        }
    }
    finding_scores = {
        "papers": {
            "paper_a": {
                "findings": [
                    {
                        "gold_index": 0,
                        "completeness": 1.0,
                        "accuracy": 1.0,
                        "reasoning": "represented",
                        "human_rationale": "represented",
                        "match": {"status": "represented_full"},
                        "field_adjudications": [
                            {
                                "status": "represented_by_transform",
                                "gold_value": 4,
                                "extraction_value": 4,
                                "extraction_expression": {
                                    "op": "sum",
                                    "rows": [0, 1],
                                    "field": "n_events",
                                },
                                "explanation": "sum two event rows",
                            },
                            {
                                "status": "represented",
                                "gold_value": "-",
                                "extraction_value": "not_reported",
                                "extraction_expression": {
                                    "op": "direct",
                                    "row": 0,
                                    "field": "effect_size_type",
                                },
                                "explanation": "concordant nonnumeric absence",
                            },
                        ],
                    }
                ]
            }
        }
    }

    report = compute_semantic_value_gate(
        finding_scores=finding_scores,
        comparisons=comparisons,
        require_structured=True,
    )

    assert report["value_gate_passed"] is True
    assert report["invalid_transform_count"] == 0


def test_semantic_value_gate_accepts_pooled_partial_and_gold_error_mismatch():
    comparisons = {
        "paper_a": {
            "paper_id": "paper_a",
            "benchmark_status": "gold_scored",
            "gold_scored": True,
            "gold_rows": 1,
            "paired_rows": [
                {
                    "gold_index": 0,
                    "gold_fields": {"Patients with Variant": 7, "Patients with Outcome": 31},
                    "extraction_candidates": [
                        {"extraction_index": 7, "n_total": 10, "n_events": 630},
                    ],
                }
            ],
        }
    }
    finding_scores = {
        "papers": {
            "paper_a": {
                "findings": [
                    {
                        "gold_index": 0,
                        "completeness": 0.5,
                        "accuracy": 1.0,
                        "reasoning": "pooled arm value is faithful but partial",
                        "human_rationale": "pooled arm value is faithful but partial",
                        "match": {"status": "represented_partial"},
                        "field_adjudications": [
                            {
                                "status": "represented_by_pooled_value",
                                "gold_value": 7,
                                "extraction_value": 10,
                                "extraction_expression": {
                                    "op": "pooled_sum",
                                    "row": 7,
                                    "field": "n_total",
                                },
                                "explanation": "Gold arm-specific value is one addend of the pooled value.",
                            },
                            {
                                "status": "gold_error",
                                "gold_value": 31,
                                "extraction_value": 630,
                                "extraction_expression": {
                                    "op": "direct",
                                    "row": 7,
                                    "field": "n_events",
                                },
                                "explanation": "Gold value is internally inconsistent; extraction is faithful.",
                            },
                        ],
                    }
                ]
            }
        }
    }

    report = compute_semantic_value_gate(
        finding_scores=finding_scores,
        comparisons=comparisons,
        require_structured=True,
    )

    assert report["value_gate_passed"] is True
    assert report["invalid_transform_count"] == 0
    assert report["value_mismatch_count"] == 0
    assert report["gold_error_count"] == 1
