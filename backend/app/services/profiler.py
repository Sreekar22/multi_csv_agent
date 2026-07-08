import re
from pathlib import Path
from typing import Any

import pandas as pd


def to_table_name(file_name: str) -> str:
    stem = Path(file_name).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    return stem or "table"


def _safe_sample(series: pd.Series, max_items: int = 5) -> list[str]:
    values = series.dropna().astype(str).head(max_items).tolist()
    return values


def _numeric_stats(series: pd.Series) -> dict[str, Any]:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return {}
    return {
        "min": float(numeric.min()),
        "max": float(numeric.max()),
        "mean": float(numeric.mean()),
    }


def profile_csv(file_path: Path, file_name: str) -> dict[str, Any]:
    df = pd.read_csv(file_path)

    row_count = int(len(df))
    column_count = int(len(df.columns))

    columns_profile = []
    total_nulls = int(df.isna().sum().sum())

    for column in df.columns:
        series = df[column]
        null_count = int(series.isna().sum())
        null_pct = float((null_count / row_count) * 100) if row_count else 0.0

        columns_profile.append(
            {
                "name": str(column),
                "inferred_dtype": str(series.dtype),
                "null_count": null_count,
                "null_pct": round(null_pct, 4),
                "unique_count": int(series.nunique(dropna=True)),
                "sample_values": _safe_sample(series),
                "numeric_stats": _numeric_stats(series),
            }
        )

    quality_summary = {
        "row_count": row_count,
        "column_count": column_count,
        "total_missing_values": total_nulls,
        "duplicate_rows": int(df.duplicated().sum()),
    }

    return {
        "file_name": file_name,
        "table_name": to_table_name(file_name),
        "rows": row_count,
        "columns": column_count,
        "columns_profile": columns_profile,
        "quality_summary": quality_summary,
    }
