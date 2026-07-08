import json
import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.agents.agentic_csv_chat import (
    generate_agentic_chat_response,
    generate_general_llm_response,
)
from app.core.config import settings
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.session import (
    SessionCreateResponse,
    SessionQualityResponse,
    SessionSchemaResponse,
    UploadFilesResponse,
    UploadedFileProfile,
)
from app.services.cleaner import clean_csv
from app.services.profiler import profile_csv
from app.services.session_store import (
    create_session,
    ensure_session_exists,
    read_manifest,
    upsert_manifest,
)

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def _wants_visual_response(message: str) -> bool:
    lower = message.lower()
    markers = [
        "chart",
        "graph",
        "plot",
        "visual",
        "visualize",
        "visualise",
        "bar chart",
        "line chart",
        "show visually",
        "trend",
        "breakdown",
        "distribution",
        "pie",
        "donut",
        "area chart",
        "scatter",
        "scatter plot",
    ]
    return any(marker in lower for marker in markers)


def _is_time_like_label(column_name: str) -> bool:
    lower = column_name.lower()
    return any(token in lower for token in ["date", "time", "month", "year", "day", "week"])


def _coerce_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    if isinstance(value, str):
        normalized = value.replace(",", "").strip()
        if not normalized:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _collect_rows_from_chart_spec(chart_spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    series = chart_spec.get("series")
    if isinstance(series, list):
        rows.extend([row for row in series if isinstance(row, dict)])

    data = chart_spec.get("data")
    if isinstance(data, list):
        rows.extend([row for row in data if isinstance(row, dict)])
    elif isinstance(data, dict):
        values = data.get("values")
        if isinstance(values, list):
            rows.extend([row for row in values if isinstance(row, dict)])

    return rows


def _metadata_from_rows(rows: list[dict[str, Any]], preferred_chart_type: str) -> dict[str, Any]:
    if not rows or not isinstance(rows[0], dict):
        return {}

    columns = [str(column) for column in rows[0].keys()]
    if not columns:
        return {}

    numeric_columns = [
        column
        for column in columns
        if any(_coerce_number(row.get(column)) is not None for row in rows)
    ]
    if not numeric_columns:
        return {}

    x_column = next((column for column in columns if column not in numeric_columns), columns[0])
    y_column = next((column for column in numeric_columns if column != x_column), numeric_columns[0])

    labels: list[str] = []
    data: list[float] = []
    for row in rows[:100]:
        number_value = _coerce_number(row.get(y_column))
        if number_value is None:
            continue
        labels.append(str(row.get(x_column, "")))
        data.append(number_value)

    if not labels or not data:
        return {}

    if preferred_chart_type in {"pie", "donut"}:
        return {
            "labels": labels,
            "data": data,
            "slices": [
                {"label": label, "value": value}
                for label, value in zip(labels, data)
            ],
        }

    if preferred_chart_type == "scatter":
        if len(numeric_columns) < 2:
            return {}
        x_scatter = numeric_columns[0]
        y_scatter = numeric_columns[1]
        points: list[dict[str, float]] = []
        for row in rows[:200]:
            x_value = _coerce_number(row.get(x_scatter))
            y_value = _coerce_number(row.get(y_scatter))
            if x_value is None or y_value is None:
                continue
            points.append({"x": x_value, "y": y_value})
        if not points:
            return {}
        return {
            "x": x_scatter,
            "y": y_scatter,
            "points": points,
        }

    return {
        "labels": labels,
        "data": data,
        "x": x_column,
        "y": y_column,
    }


def _normalize_chart_contract(
    message: str,
    answer_text: str,
    table_preview: list[dict[str, Any]],
    chart_spec: Any,
) -> dict[str, Any]:
    wants_visual = _wants_visual_response(message)
    existing = chart_spec if isinstance(chart_spec, dict) else {}
    supported_types = {"pie", "donut", "bar", "line", "area", "scatter", "kpi", "none"}

    def _infer_type_from_message() -> str:
        lower_message = message.lower()
        if any(token in lower_message for token in ["scatter", "correlation", "relationship"]):
            return "scatter"
        if any(token in lower_message for token in ["area", "cumulative", "filled trend"]):
            return "area"
        if any(token in lower_message for token in ["donut", "doughnut"]):
            return "donut"
        if any(token in lower_message for token in ["distribution", "share", "composition", "split", "pie"]):
            return "pie"
        if any(token in lower_message for token in ["trend", "over time", "timeline"]):
            return "line"
        return "bar"

    candidate_rows = _collect_rows_from_chart_spec(existing)
    if not candidate_rows and isinstance(table_preview, list):
        candidate_rows = [row for row in table_preview if isinstance(row, dict)]

    raw_chart_type = existing.get("chart_type")
    raw_title = existing.get("title")
    raw_metadata = existing.get("metadata")

    if isinstance(raw_chart_type, list):
        chart_type_list = [str(item).strip().lower() for item in raw_chart_type]
        title_list = raw_title if isinstance(raw_title, list) else []
        metadata_list = raw_metadata if isinstance(raw_metadata, list) else []

        normalized_types: list[str] = []
        normalized_titles: list[str] = []
        normalized_metadata: list[dict[str, Any]] = []

        for index, chart_type_item in enumerate(chart_type_list):
            chart_type = chart_type_item if chart_type_item in supported_types else "none"
            if chart_type == "none" and wants_visual:
                chart_type = _infer_type_from_message()

            metadata_item = (
                metadata_list[index]
                if index < len(metadata_list) and isinstance(metadata_list[index], dict)
                else {}
            )
            title_item = (
                str(title_list[index]).strip()
                if index < len(title_list)
                else ""
            )

            if chart_type != "none" and not metadata_item:
                metadata_item = _metadata_from_rows(candidate_rows, chart_type)

            if chart_type == "none" or not metadata_item:
                continue

            normalized_types.append(chart_type)
            normalized_titles.append(title_item or f"Chart {index + 1}")
            normalized_metadata.append(metadata_item)

        if normalized_types:
            return {
                "chart_type": normalized_types,
                "title": normalized_titles,
                "metadata": normalized_metadata,
                "source": "normalized_contract_v3",
                "reason": "visual_requested",
            }

        return {
            "chart_type": "none",
            "title": "",
            "metadata": {},
            "source": "normalized_contract_v3",
            "reason": "none",
        }

    raw_type = str(raw_chart_type or existing.get("type") or "").strip().lower()
    chart_type = raw_type if raw_type in supported_types else "none"

    if chart_type == "none" and wants_visual:
        chart_type = _infer_type_from_message()

    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    title = str(raw_title or "").strip() or "Chart"

    if chart_type != "none" and not metadata:
        metadata = _metadata_from_rows(candidate_rows, chart_type)

    if chart_type == "none" and not wants_visual:
        metadata = {}

    if chart_type != "none" and not metadata:
        chart_type = "none"

    return {
        "chart_type": chart_type,
        "title": title if chart_type != "none" else "",
        "metadata": metadata,
        "source": "normalized_contract_v3",
        "reason": "visual_requested" if wants_visual and chart_type != "none" else "none",
    }


def _build_chart_spec_from_preview(rows: list[dict], question: str) -> dict | None:
    if not rows or not isinstance(rows[0], dict):
        return None

    columns = [str(column) for column in rows[0].keys()]
    if not columns:
        return None

    numeric_columns = [
        column
        for column in columns
        if any(_coerce_number(row.get(column)) is not None for row in rows)
    ]
    if not numeric_columns:
        return None

    if len(columns) == 1:
        metric = numeric_columns[0]
        first_value = _coerce_number(rows[0].get(metric))
        if first_value is None:
            return None
        return {
            "chart_type": "bar",
            "x": "label",
            "y": "value",
            "title": metric.replace("_", " ").title(),
            "series": [{"label": metric.replace("_", " ").title(), "value": first_value}],
        }

    x_column = columns[0]
    y_column = next((column for column in numeric_columns if column != x_column), numeric_columns[0])
    if x_column == y_column and len(columns) > 1:
        x_column = next((column for column in columns if column != y_column), x_column)

    if x_column == y_column:
        first_value = _coerce_number(rows[0].get(y_column))
        if first_value is None:
            return None
        return {
            "chart_type": "bar",
            "x": "label",
            "y": "value",
            "title": question.strip() or y_column.replace("_", " ").title(),
            "series": [{"label": y_column.replace("_", " ").title(), "value": first_value}],
        }

    chart_type = "line" if _is_time_like_label(x_column) else "bar"
    return {
        "chart_type": chart_type,
        "x": x_column,
        "y": y_column,
        "title": f"{y_column.replace('_', ' ').title()} by {x_column.replace('_', ' ').title()}",
        "series": rows[:100],
    }


def _apply_visual_preference(message: str, response: dict) -> dict:
    if response.get("chart_spec") or not _wants_visual_response(message):
        return response

    preview_rows = response.get("table_preview", [])
    if not isinstance(preview_rows, list):
        return response

    chart_spec = _build_chart_spec_from_preview(preview_rows, message)
    if chart_spec is None:
        return response

    response["chart_spec"] = chart_spec
    response["answer_type"] = "text_and_chart"
    return response


@router.post("", response_model=SessionCreateResponse)
def create_session_endpoint() -> SessionCreateResponse:
    payload = create_session()
    return SessionCreateResponse(
        session_id=payload["session_id"],
        created_at=payload["created_at"],
    )


@router.post("/{session_id}/files", response_model=UploadFilesResponse)
async def upload_files(
    session_id: str,
    clean_data: bool = Form(False),
    files: list[UploadFile] = File(...),
) -> UploadFilesResponse:
    if not files:
        raise HTTPException(status_code=400, detail="At least one CSV file is required")

    if len(files) > settings.max_upload_files:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.max_upload_files} files allowed per request",
        )

    try:
        raw_session_dir, profile_session_dir = ensure_session_exists(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    processed_session_dir = settings.processed_data_dir / session_id
    processed_session_dir.mkdir(parents=True, exist_ok=True)

    uploaded_files: list[UploadedFileProfile] = []
    manifest_updates: list[dict] = []

    for upload in files:
        file_name = upload.filename or "uploaded.csv"
        if not file_name.lower().endswith(".csv"):
            raise HTTPException(status_code=400, detail=f"Only CSV files are allowed: {file_name}")

        file_bytes = await upload.read()
        size_mb = len(file_bytes) / (1024 * 1024)
        if size_mb > settings.max_file_size_mb:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File '{file_name}' exceeds size limit of "
                    f"{settings.max_file_size_mb} MB"
                ),
            )

        raw_file_path = raw_session_dir / file_name
        processed_file_path = processed_session_dir / file_name
        raw_file_path.write_bytes(file_bytes)

        source_file_path = raw_file_path
        cleaning_summary: dict[str, Any] = {}
        raw_quality_summary: dict[str, Any] | None = None
        if clean_data:
            try:
                raw_profile_payload = profile_csv(raw_file_path, file_name)
                raw_quality_summary = raw_profile_payload.get("quality_summary", {})
                cleaning_summary = clean_csv(raw_file_path, processed_file_path)
                source_file_path = processed_file_path
            except Exception as exc:
                raw_file_path.unlink(missing_ok=True)
                processed_file_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to clean CSV '{file_name}': {exc}",
                ) from exc

        try:
            profile_payload = profile_csv(source_file_path, file_name)
        except Exception as exc:
            raw_file_path.unlink(missing_ok=True)
            processed_file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to parse CSV '{file_name}': {exc}",
            ) from exc

        if clean_data and raw_quality_summary is not None:
            cleaned_quality_summary = profile_payload.get("quality_summary", {})
            cleaning_summary["verified_quality_before"] = {
                "rows": int(raw_quality_summary.get("row_count", 0)),
                "missing_values": int(raw_quality_summary.get("total_missing_values", 0)),
                "duplicate_rows": int(raw_quality_summary.get("duplicate_rows", 0)),
            }
            cleaning_summary["verified_quality_after"] = {
                "rows": int(cleaned_quality_summary.get("row_count", 0)),
                "missing_values": int(cleaned_quality_summary.get("total_missing_values", 0)),
                "duplicate_rows": int(cleaned_quality_summary.get("duplicate_rows", 0)),
            }

        profile_path = profile_session_dir / f"{Path(file_name).stem}.profile.json"
        profile_path.write_text(json.dumps(profile_payload, indent=2), encoding="utf-8")

        profile_record = UploadedFileProfile(
            file_name=file_name,
            table_name=profile_payload["table_name"],
            rows=profile_payload["rows"],
            columns=profile_payload["columns"],
            profile_path=str(profile_path),
            cleaned=clean_data,
            cleaning_summary=cleaning_summary if clean_data else {},
        )
        uploaded_files.append(profile_record)
        manifest_updates.append(
            {
                **profile_record.model_dump(),
                "active_file_name": file_name if clean_data else file_name,
                "source_file": "processed" if clean_data else "raw",
            }
        )

    upsert_manifest(session_id, manifest_updates)

    return UploadFilesResponse(session_id=session_id, uploaded_files=uploaded_files)


@router.get("/{session_id}/schema", response_model=SessionSchemaResponse)
def get_schema(session_id: str) -> SessionSchemaResponse:
    try:
        _, profile_session_dir = ensure_session_exists(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    manifest = read_manifest(session_id)
    files_payload = []

    for file_meta in manifest.get("files", []):
        profile_name = f"{Path(file_meta['file_name']).stem}.profile.json"
        profile_path = profile_session_dir / profile_name
        if not profile_path.exists():
            continue

        profile_payload = json.loads(profile_path.read_text(encoding="utf-8"))
        profile_payload["cleaned"] = bool(file_meta.get("cleaned", False))
        profile_payload["cleaning_summary"] = file_meta.get("cleaning_summary", {})
        files_payload.append(profile_payload)

    return SessionSchemaResponse(session_id=session_id, files=files_payload)


@router.get("/{session_id}/quality-report", response_model=SessionQualityResponse)
def get_quality_report(session_id: str) -> SessionQualityResponse:
    schema = get_schema(session_id)

    report = {
        "file_count": len(schema.files),
        "total_rows": sum(item.rows for item in schema.files),
        "total_columns": sum(item.columns for item in schema.files),
        "total_missing_values": sum(
            int(item.quality_summary.get("total_missing_values", 0))
            for item in schema.files
        ),
        "total_duplicate_rows": sum(
            int(item.quality_summary.get("duplicate_rows", 0))
            for item in schema.files
        ),
        "files": [
            {
                "file_name": item.file_name,
                "table_name": item.table_name,
                "rows": item.rows,
                "columns": item.columns,
                "missing_values": int(item.quality_summary.get("total_missing_values", 0)),
                "duplicate_rows": int(item.quality_summary.get("duplicate_rows", 0)),
            }
            for item in schema.files
        ],
    }

    return SessionQualityResponse(session_id=session_id, quality_report=report)


@router.post("/{session_id}/chat", response_model=ChatResponse)
def chat(session_id: str, payload: ChatRequest) -> ChatResponse:
    try:
        ensure_session_exists(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    response = None
    if settings.agentic_chat_enabled:
        response = generate_agentic_chat_response(
            session_id=session_id,
            question=payload.message,
        )

    if response is None:
        response = generate_general_llm_response(
            session_id=session_id,
            question=payload.message,
        )

    if response is None:
        response = {
            "answer_type": "text",
            "answer_text": (
                "I could not reach the LLM service for this request. "
                "Please verify backend LLM configuration and try again."
            ),
            "table_preview": [],
            "chart_spec": None,
            "follow_up_suggestions": [],
            "context_applied": False,
            "provenance": {
                "mode": "llm_unavailable",
            },
        }

    response = _apply_visual_preference(payload.message, response)

    table_preview = response.get("table_preview", [])
    if not isinstance(table_preview, list):
        table_preview = []

    normalized_chart = _normalize_chart_contract(
        message=payload.message,
        answer_text=str(response.get("answer_text") or ""),
        table_preview=table_preview,
        chart_spec=response.get("chart_spec"),
    )

    normalized_chart_type = normalized_chart.get("chart_type")
    has_visual_chart = (
        isinstance(normalized_chart_type, list)
        and any(str(item).strip().lower() != "none" for item in normalized_chart_type)
    ) or (
        isinstance(normalized_chart_type, str)
        and normalized_chart_type != "none"
    )

    return ChatResponse(
        session_id=session_id,
        question=payload.message,
        answer_type=(
            "text_and_chart"
            if has_visual_chart
            else "text_and_table"
            if table_preview
            else "text"
        ),
        answer_text=response["answer_text"],
        table_preview=table_preview,
        chart_spec=normalized_chart,
        follow_up_suggestions=response.get("follow_up_suggestions", []),
        context_applied=response.get("context_applied", False),
        provenance=response.get("provenance", {}),
    )
