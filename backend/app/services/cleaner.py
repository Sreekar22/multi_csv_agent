from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


_MISSING_TOKENS = ["", "na", "n/a", "null", "none", "nan", "-"]


def _trim_string_cells(frame: pd.DataFrame) -> pd.DataFrame:
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        frame[column] = frame[column].apply(
            lambda value: value.strip() if isinstance(value, str) else value
        )
    return frame


def _coerce_numeric_columns(frame: pd.DataFrame, threshold: float = 0.8) -> tuple[pd.DataFrame, list[str]]:
    converted_columns: list[str] = []
    for column in frame.columns:
        series = frame[column]
        if series.dtype.kind in {"i", "u", "f"}:
            continue

        coerced = pd.to_numeric(series, errors="coerce")
        original_non_null = int(series.notna().sum())
        if original_non_null == 0:
            continue

        parsable_ratio = float(coerced.notna().sum() / original_non_null)
        if parsable_ratio >= threshold:
            frame[column] = coerced
            converted_columns.append(str(column))

    return frame, converted_columns


def _fill_missing_values(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    filled_by_column: dict[str, int] = {}
    for column in frame.columns:
        missing_before = int(frame[column].isna().sum())
        if missing_before == 0:
            continue

        if frame[column].dtype.kind in {"i", "u", "f"}:
            median_value = frame[column].median()
            fill_value = 0.0 if pd.isna(median_value) else float(median_value)
            frame[column] = frame[column].fillna(fill_value)
        else:
            mode_series = frame[column].mode(dropna=True)
            fill_value = "Unknown" if mode_series.empty else mode_series.iloc[0]
            frame[column] = frame[column].fillna(fill_value)

        missing_after = int(frame[column].isna().sum())
        filled_by_column[str(column)] = max(0, missing_before - missing_after)

    return frame, filled_by_column


def _clip_outliers_iqr(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    clipped_counts: dict[str, int] = {}
    numeric_columns = frame.select_dtypes(include=["number"]).columns

    for column in numeric_columns:
        series = frame[column]
        if series.empty:
            continue

        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        if pd.isna(iqr) or iqr == 0:
            continue

        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr

        below = int((series < lower).sum())
        above = int((series > upper).sum())
        clipped = below + above
        if clipped == 0:
            continue

        frame[column] = series.clip(lower=lower, upper=upper)
        clipped_counts[str(column)] = clipped

    return frame, clipped_counts


def clean_csv(input_path: Path, output_path: Path) -> dict[str, Any]:
    frame = pd.read_csv(input_path)

    rows_before = int(len(frame))
    nulls_before = int(frame.isna().sum().sum())

    frame.columns = [str(column).strip() for column in frame.columns]
    frame = _trim_string_cells(frame)
    frame = frame.replace(_MISSING_TOKENS, pd.NA)

    duplicates_before = int(frame.duplicated().sum())
    frame = frame.drop_duplicates(ignore_index=True)

    frame, numeric_converted = _coerce_numeric_columns(frame)
    frame, filled_by_column = _fill_missing_values(frame)
    frame, clipped_by_column = _clip_outliers_iqr(frame)

    rows_after = int(len(frame))
    nulls_after = int(frame.isna().sum().sum())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)

    return {
        "rows_before": rows_before,
        "rows_after": rows_after,
        "duplicates_removed": max(0, rows_before - rows_after),
        "missing_values_before": nulls_before,
        "missing_values_after": nulls_after,
        "numeric_columns_converted": numeric_converted,
        "filled_missing_by_column": filled_by_column,
        "outliers_clipped_by_column": clipped_by_column,
        "cleaning_applied": True,
    }
