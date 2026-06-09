from __future__ import annotations

import math

import numpy as np
import pytest

from app.service.analysis.limit_tables import (
    LIMIT_KIND_LOWER,
    LIMIT_KIND_UPPER,
    EvaluatedLimits,
    LimitPoint,
    LimitTable,
    evaluate_limit_tables,
    limit_lines_for_chart,
    normalize_limit_tables,
    normalize_limit_tables_for_storage,
    validate_limit_tables,
)


def test_legacy_rows_normalize_to_lower_and_upper_tables():
    tables = normalize_limit_tables(
        [
            {"freq": 1000.0, "lower": 0.0, "upper": 100.0},
            {"freq": 2000.0, "lower": 10.0, "upper": 90.0},
        ],
        metric="fft",
    )

    assert [table.kind for table in tables] == [LIMIT_KIND_LOWER, LIMIT_KIND_UPPER]
    assert [point.limit for point in tables[0].points] == [0.0, 10.0]
    assert [point.limit for point in tables[1].points] == [100.0, 90.0]
    assert [point.freq for point in tables[0].points] == [1000.0, 2000.0]
    assert [point.source_index for point in tables[0].points] == [0, 1]


def test_grouped_tables_normalize_and_storage_cleanup_preserves_duplicate_points():
    raw = [
        {
            "kind": "upper",
            "points": [
                {"freq": 2000.0, "limit": 100.0},
                {"freq": 1000.0, "limit": 90.0},
                {"freq": 2000.0, "limit": 50.0},
            ],
        },
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 0.0},
                {"freq": 3000.0, "limit": 10.0},
            ],
        },
    ]

    tables = normalize_limit_tables(raw, metric="fft")

    assert [table.kind for table in tables] == [LIMIT_KIND_UPPER, LIMIT_KIND_LOWER]
    assert [(point.freq, point.limit) for point in tables[0].points] == [
        (1000.0, 90.0),
        (2000.0, 100.0),
        (2000.0, 50.0),
    ]
    assert normalize_limit_tables_for_storage(raw, metric="fft") == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 90.0},
                {"freq": 2000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 50.0},
            ],
        },
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 0.0},
                {"freq": 3000.0, "limit": 10.0},
            ],
        },
    ]


def test_already_normalized_tables_are_accepted():
    table = LimitTable(
        kind=LIMIT_KIND_UPPER,
        source_index=2,
        points=(
            LimitPoint(freq=2000.0, limit=80.0, source_index=2, point_index=1),
            LimitPoint(freq=1000.0, limit=90.0, source_index=2, point_index=0),
        ),
    )

    tables = normalize_limit_tables([table], metric="fft")

    assert [point.freq for point in tables[0].points] == [1000.0, 2000.0]
    assert tables[0].source_index == 2


def test_mixed_legacy_and_grouped_shape_is_invalid():
    errors = validate_limit_tables(
        [
            {"freq": 1000.0, "lower": 0.0, "upper": 100.0},
            {"kind": "upper", "points": [{"freq": 1000.0, "limit": 100.0}]},
        ],
        metric="fft",
        label="FFT",
    )

    assert any("混合" in error or "格式" in error for error in errors)


@pytest.mark.parametrize("kind", [LIMIT_KIND_UPPER, LIMIT_KIND_LOWER])
def test_one_sided_fft_tables_are_valid(kind: str):
    errors = validate_limit_tables(
        [
            {
                "kind": kind,
                "points": [
                    {"freq": 1000.0, "limit": 50.0},
                    {"freq": 2000.0, "limit": 60.0},
                ],
            }
        ],
        metric="fft",
        label="FFT",
    )

    assert errors == []


def test_multiple_tables_per_kind_combine_with_conservative_bounds():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 3000.0, "limit": 100.0},
                ],
            },
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 90.0},
                    {"freq": 3000.0, "limit": 70.0},
                ],
            },
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 0.0},
                    {"freq": 3000.0, "limit": 10.0},
                ],
            },
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 5.0},
                    {"freq": 3000.0, "limit": 5.0},
                ],
            },
        ],
        metric="fft",
    )

    evaluated = evaluate_limit_tables(
        tables,
        np.asarray([1500.0, 2500.0], dtype=np.float64),
    )

    assert evaluated.upper.tolist() == pytest.approx([85.0, 75.0])
    assert evaluated.lower.tolist() == pytest.approx([5.0, 7.5])
    assert evaluated.active.tolist() == [True, True]


def test_duplicate_frequency_points_form_vertical_step_for_upper_limits():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 50.0},
                    {"freq": 3000.0, "limit": 50.0},
                ],
            }
        ],
        metric="fft",
    )

    evaluated = evaluate_limit_tables(
        tables,
        np.asarray([1500.0, 2000.0, 2500.0], dtype=np.float64),
    )

    assert isinstance(evaluated, EvaluatedLimits)
    assert evaluated.upper.tolist() == pytest.approx([100.0, 50.0, 50.0])
    assert np.isnan(evaluated.lower).all()
    assert limit_lines_for_chart(tables) == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 100.0},
                {"freq_hz": 2000.0, "limit_db": 100.0},
                {"freq_hz": 2000.0, "limit_db": 50.0},
                {"freq_hz": 3000.0, "limit_db": 50.0},
            ],
        }
    ]


def test_absolute_golden_mode_evaluates_bounds_with_matching_levels():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 10.0},
                    {"freq": 3000.0, "limit": 30.0},
                ],
            },
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": -20.0},
                    {"freq": 3000.0, "limit": -10.0},
                ],
            },
        ],
        metric="fft",
    )

    evaluated = evaluate_limit_tables(
        tables,
        frequencies=np.asarray([1000.0, 2000.0, 3000.0], dtype=np.float64),
        golden_levels=np.asarray([60.0, 70.0, 80.0], dtype=np.float64),
        mode="absolute",
    )

    assert evaluated.upper.tolist() == pytest.approx([70.0, 90.0, 110.0])
    assert evaluated.lower.tolist() == pytest.approx([40.0, 55.0, 70.0])
    assert evaluated.active.tolist() == [True, True, True]


def test_absolute_golden_mode_projects_chart_lines_with_interpolated_levels():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 10.0},
                    {"freq": 2000.0, "limit": 10.0},
                    {"freq": 2000.0, "limit": 20.0},
                    {"freq": 3000.0, "limit": 20.0},
                ],
            }
        ],
        metric="fft",
    )

    lines = limit_lines_for_chart(
        tables,
        frequencies=np.asarray([1000.0, 3000.0], dtype=np.float64),
        golden_levels=np.asarray([60.0, 80.0], dtype=np.float64),
        mode="absolute",
    )

    assert lines == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 70.0},
                {"freq_hz": 2000.0, "limit_db": 80.0},
                {"freq_hz": 2000.0, "limit_db": 90.0},
                {"freq_hz": 3000.0, "limit_db": 100.0},
            ],
        }
    ]


@pytest.mark.parametrize("mode", ["off", "delta"])
def test_off_and_delta_golden_modes_preserve_offset_bounds(mode: str):
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 10.0},
                    {"freq": 3000.0, "limit": 30.0},
                ],
            }
        ],
        metric="fft",
    )

    evaluated = evaluate_limit_tables(
        tables,
        np.asarray([1000.0, 2000.0, 3000.0], dtype=np.float64),
        golden_levels=np.asarray([60.0, 70.0, 80.0], dtype=np.float64),
        mode=mode,
    )
    lines = limit_lines_for_chart(
        tables,
        frequencies=np.asarray([1000.0, 3000.0], dtype=np.float64),
        golden_levels=np.asarray([60.0, 80.0], dtype=np.float64),
        mode=mode,
    )

    assert evaluated.upper.tolist() == pytest.approx([10.0, 20.0, 30.0])
    assert lines == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 10.0},
                {"freq_hz": 3000.0, "limit_db": 30.0},
            ],
        }
    ]


def test_invalid_golden_mode_is_rejected():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 10.0},
                    {"freq": 3000.0, "limit": 30.0},
                ],
            }
        ],
        metric="fft",
    )

    with pytest.raises(ValueError, match="mode"):
        evaluate_limit_tables(
            tables,
            np.asarray([1000.0], dtype=np.float64),
            mode="unsupported",
        )
    with pytest.raises(ValueError, match="mode"):
        limit_lines_for_chart(tables, mode="unsupported")


def test_exact_duplicate_frequency_uses_conservative_value_for_each_kind():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 90.0},
                    {"freq": 2000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 50.0},
                    {"freq": 3000.0, "limit": 50.0},
                ],
            },
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 0.0},
                    {"freq": 2000.0, "limit": 10.0},
                    {"freq": 2000.0, "limit": 30.0},
                    {"freq": 3000.0, "limit": 30.0},
                ],
            },
        ],
        metric="fft",
    )

    evaluated = evaluate_limit_tables(tables, np.asarray([2000.0], dtype=np.float64))

    assert evaluated.upper.tolist() == pytest.approx([50.0])
    assert evaluated.lower.tolist() == pytest.approx([30.0])


def test_outside_table_span_is_inactive():
    tables = normalize_limit_tables(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 80.0},
                    {"freq": 2000.0, "limit": 70.0},
                ],
            }
        ],
        metric="fft",
    )

    evaluated = evaluate_limit_tables(
        tables,
        np.asarray([500.0, 1500.0, 2500.0], dtype=np.float64),
    )

    assert math.isnan(evaluated.upper[0])
    assert evaluated.upper[1] == pytest.approx(75.0)
    assert math.isnan(evaluated.upper[2])
    assert evaluated.active.tolist() == [False, True, False]


def test_crossing_validation_checks_intersections_between_segments():
    errors = validate_limit_tables(
        [
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 0.0},
                    {"freq": 3000.0, "limit": 100.0},
                ],
            },
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 80.0},
                    {"freq": 3000.0, "limit": 20.0},
                ],
            },
        ],
        metric="fft",
        label="FFT",
    )

    assert any("下限" in error and "上限" in error for error in errors)


def test_crossing_validation_checks_exact_duplicate_frequency_values():
    errors = validate_limit_tables(
        [
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 0.0},
                    {"freq": 2000.0, "limit": 60.0},
                    {"freq": 2000.0, "limit": 80.0},
                    {"freq": 3000.0, "limit": 80.0},
                ],
            },
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 70.0},
                    {"freq": 2000.0, "limit": 90.0},
                    {"freq": 3000.0, "limit": 90.0},
                ],
            },
        ],
        metric="fft",
        label="FFT",
    )

    assert any("下限" in error and "上限" in error for error in errors)


def test_boundary_equality_between_lower_and_upper_is_invalid():
    errors = validate_limit_tables(
        [
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 50.0},
                    {"freq": 2000.0, "limit": 50.0},
                ],
            },
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 50.0},
                    {"freq": 2000.0, "limit": 60.0},
                ],
            },
        ],
        metric="fft",
        label="FFT",
    )

    assert any("下限" in error and "上限" in error for error in errors)


def test_empty_limits_list_disables_limits():
    assert normalize_limit_tables([], metric="octave") == []
    assert normalize_limit_tables_for_storage([], metric="fft") == []
    assert validate_limit_tables([], metric="octave", label="倍频程") == []


def test_explicit_none_limits_are_invalid():
    errors = validate_limit_tables(None, metric="octave", label="倍频程")

    assert any("列表" in error for error in errors)
    with pytest.raises(ValueError, match="列表"):
        normalize_limit_tables(None, metric="fft", label="FFT")
    with pytest.raises(ValueError, match="列表"):
        normalize_limit_tables_for_storage(None, metric="fft", label="FFT")


@pytest.mark.parametrize(
    ("metric", "label"),
    [
        ("fft", "FFT"),
        ("octave", "倍频程"),
    ],
)
def test_empty_grouped_tables_are_ignored_before_validation_and_storage(
    metric: str,
    label: str,
):
    raw = [
        {"kind": "upper", "points": []},
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 10.0},
                {"freq": 2000.0, "limit": 20.0},
            ],
        },
    ]

    assert validate_limit_tables(raw, metric=metric, label=label) == []
    tables = normalize_limit_tables(raw, metric=metric, label=label)

    assert [table.kind for table in tables] == [LIMIT_KIND_LOWER]
    assert [(point.freq, point.limit) for point in tables[0].points] == [
        (1000.0, 10.0),
        (2000.0, 20.0),
    ]
    assert normalize_limit_tables_for_storage(raw, metric=metric, label=label) == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 10.0},
                {"freq": 2000.0, "limit": 20.0},
            ],
        }
    ]


def test_malformed_non_empty_inputs_report_validation_errors():
    raw = [
        "not a table",
        {"kind": "middle", "points": [{"freq": 1000.0, "limit": 50.0}]},
        {"kind": "upper", "points": "not a point list"},
        {"kind": "lower", "points": [{"freq": True, "limit": 0.0}]},
        {"kind": "upper", "points": [{"freq": 1000.0, "limit": float("nan")}]},
        {"kind": "upper", "points": [{"freq": -1.0, "limit": 10.0}]},
    ]

    errors = validate_limit_tables(raw, metric="fft", label="FFT")

    assert len(errors) >= 6
    assert any("格式" in error for error in errors)
    assert any("类型" in error or "kind" in error for error in errors)
    assert any("频率" in error for error in errors)
    assert any("限值" in error for error in errors)


def test_malformed_non_empty_grouped_table_is_not_dropped():
    raw = [
        {
            "kind": "upper",
            "points": [{"freq": 1000.0, "limit": "bad"}],
        }
    ]

    errors = validate_limit_tables(raw, metric="fft", label="FFT")

    assert any("限值" in error for error in errors)
    with pytest.raises(ValueError, match="限值"):
        normalize_limit_tables(raw, metric="fft", label="FFT")


def test_non_list_non_empty_input_reports_validation_error():
    errors = validate_limit_tables({"kind": "upper"}, metric="fft", label="FFT")

    assert any("列表" in error for error in errors)


def test_invalid_legacy_numeric_values_are_rejected():
    errors = validate_limit_tables(
        [
            {"freq": 1000.0, "lower": False, "upper": 100.0},
            {"freq": float("inf"), "lower": 0.0, "upper": 100.0},
        ],
        metric="fft",
        label="FFT",
    )

    assert any("下限" in error for error in errors)
    assert any("频率" in error for error in errors)


@pytest.mark.parametrize(
    "metric,label",
    [
        ("fft", "FFT"),
        ("octave", "倍频程"),
    ],
)
def test_grouped_tables_require_two_distinct_frequencies(metric: str, label: str):
    raw = [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 80.0},
                {"freq": 1000.0, "limit": 70.0},
            ],
        }
    ]

    errors = validate_limit_tables(raw, metric=metric, label=label)

    assert any("至少" in error and "两个" in error for error in errors)


def test_octave_one_point_table_requires_two_distinct_frequencies():
    raw = [
        {
            "kind": "upper",
            "points": [{"freq": 1000.0, "limit": 80.0}],
        }
    ]

    errors = validate_limit_tables(raw, metric="octave", label="倍频程")

    assert any("至少" in error and "两个" in error for error in errors)


def test_octave_two_distinct_frequencies_are_valid():
    raw = [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 80.0},
                {"freq": 2000.0, "limit": 70.0},
            ],
        }
    ]

    assert validate_limit_tables(raw, metric="octave", label="倍频程") == []


def test_invalid_metric_is_reported_by_validation_and_rejected_by_normalization():
    errors = validate_limit_tables(
        [{"kind": "upper", "points": [{"freq": 1000.0, "limit": 80.0}]}],
        metric="narrowband",
        label="窄带",
    )

    assert any("metric" in error or "指标" in error for error in errors)
    with pytest.raises(ValueError):
        normalize_limit_tables(
            [{"kind": "upper", "points": [{"freq": 1000.0, "limit": 80.0}]}],
            metric="narrowband",
        )
