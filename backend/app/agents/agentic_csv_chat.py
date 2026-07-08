from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import ssl
from dataclasses import dataclass
from typing import Any

import duckdb
import httpx

openai_async_client_cls: Any = None
try:
    from openai import AsyncOpenAI as _OpenAIAsyncClient

    openai_async_client_cls = _OpenAIAsyncClient
except Exception:
    pass

from app.core.config import settings
from app.services.session_store import (
    ensure_session_exists,
    read_chat_context,
    read_manifest,
    upsert_chat_context,
)

try:
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool
    from google.genai import types as genai_types

    ADK_AVAILABLE = True
except Exception:
    ADK_AVAILABLE = False


@dataclass
class TableProfile:
    table_name: str
    quoted_table: str
    row_count: int
    columns: list[str]


@dataclass
class RelationshipCandidate:
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    overlap_ratio: float
    overlap_count: int
    left_coverage: float
    right_coverage: float
    confidence: float
    match_reason: str


def _quote_identifier(identifier: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", identifier)
    return f'"{safe}"'


def _register_session_tables(conn: duckdb.DuckDBPyConnection, session_id: str) -> dict[str, str]:
    raw_session_dir, _ = ensure_session_exists(session_id)
    processed_session_dir = settings.processed_data_dir / session_id
    manifest = read_manifest(session_id)

    table_map: dict[str, str] = {}
    for item in manifest.get("files", []):
        table_name = str(item.get("table_name") or "").strip()
        file_name = str(item.get("file_name") or "").strip()
        source_file = str(item.get("source_file") or "raw").strip().lower()
        if not table_name or not file_name:
            continue

        if source_file == "processed":
            file_path = processed_session_dir / file_name
            if not file_path.exists():
                file_path = raw_session_dir / file_name
        else:
            file_path = raw_session_dir / file_name
        if not file_path.exists():
            continue

        quoted_table = _quote_identifier(table_name)
        escaped_path = str(file_path).replace("'", "''")
        conn.execute(
            f"CREATE OR REPLACE VIEW {quoted_table} AS "
            f"SELECT * FROM read_csv_auto('{escaped_path}', HEADER=TRUE)"
        )
        table_map[table_name] = quoted_table

    return table_map


def _to_preview(df: Any, limit: int = 20) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = df.head(limit).to_dict(orient="records")
    return [_json_safe(row) for row in rows]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]

    # Handles pandas.Timestamp, datetime, numpy scalar/date types, Decimal, etc.
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass

    return str(value)


def _normalize_table_preview(table_preview: Any, max_rows: int = 200) -> list[dict[str, Any]]:
    if not isinstance(table_preview, list):
        return []

    object_rows: list[dict[str, Any]] = [row for row in table_preview if isinstance(row, dict)]
    if not object_rows:
        return []

    normalized_keys: list[str] = []
    for row in object_rows:
        for key in row.keys():
            key_name = str(key)
            if key_name not in normalized_keys:
                normalized_keys.append(key_name)

    normalized_rows: list[dict[str, Any]] = []
    for row in object_rows[:max_rows]:
        normalized_row: dict[str, Any] = {}
        for key in normalized_keys:
            normalized_row[key] = _json_safe(row.get(key)) if key in row else None
        normalized_rows.append(normalized_row)

    return normalized_rows


def _is_numeric_type(type_name: str) -> bool:
    lower = type_name.lower()
    return any(token in lower for token in ["int", "double", "float", "decimal", "numeric"])


def _is_time_like(name: str) -> bool:
    lower = name.lower()
    return any(token in lower for token in ["date", "time", "month", "year"])


def _build_table_profiles(
    conn: duckdb.DuckDBPyConnection, table_map: dict[str, str]
) -> list[TableProfile]:
    profiles: list[TableProfile] = []
    for table_name, quoted_table in table_map.items():
        info_rows = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        columns = [str(row[1]) for row in info_rows]
        count_row = conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()
        row_count = int(count_row[0]) if count_row else 0
        profiles.append(
            TableProfile(
                table_name=table_name,
                quoted_table=quoted_table,
                row_count=row_count,
                columns=columns,
            )
        )
    return profiles


def _likely_key_column(column: str) -> bool:
    lower = column.lower()
    return lower.endswith("_id") or lower in {"id", "uuid", "key"}


def _normalize_column_name(column: str) -> str:
    lowered = re.sub(r"[^a-zA-Z0-9]+", "_", column.lower()).strip("_")
    if lowered.endswith("_id"):
        lowered = lowered[: -len("_id")]
    if lowered.endswith("s") and len(lowered) > 3:
        lowered = lowered[:-1]
    return lowered


def _relationship_quality_summary(relationships: list[RelationshipCandidate]) -> dict[str, Any]:
    if not relationships:
        return {
            "level": "low",
            "total": 0,
            "high_confidence": 0,
            "avg_confidence": 0.0,
            "note": "No reliable cross-table links detected; multi-table conclusions may be weak.",
        }

    avg_conf = sum(item.confidence for item in relationships) / len(relationships)
    high_conf = sum(1 for item in relationships if item.confidence >= 0.75)

    if high_conf >= 3 or avg_conf >= 0.75:
        level = "high"
        note = "Cross-table linkage quality is strong for multi-table analysis."
    elif high_conf >= 1 or avg_conf >= 0.55:
        level = "medium"
        note = "Cross-table linkage quality is moderate; interpret joined metrics with some caution."
    else:
        level = "low"
        note = "Cross-table linkage is weak; prefer single-table analysis or validate join keys first."

    return {
        "level": level,
        "total": len(relationships),
        "high_confidence": high_conf,
        "avg_confidence": round(avg_conf, 3),
        "note": note,
    }


def _join_confidence_threshold(schema_payload: dict[str, Any]) -> float:
    quality = schema_payload.get("relationship_quality", {}) if isinstance(schema_payload, dict) else {}
    level = str(quality.get("level", "")).lower()
    avg_conf = float(quality.get("avg_confidence", 0.0) or 0.0)
    high_conf = int(quality.get("high_confidence", 0) or 0)

    if level == "high":
        return 0.6 if avg_conf >= 0.85 and high_conf >= 5 else 0.65
    if level == "medium":
        return 0.7
    return 0.78


def _infer_relationships(
    conn: duckdb.DuckDBPyConnection, profiles: list[TableProfile]
) -> list[RelationshipCandidate]:
    relationships: list[RelationshipCandidate] = []

    for i, left in enumerate(profiles):
        for right in profiles[i + 1 :]:
            for left_col in left.columns:
                for right_col in right.columns:
                    same_name = left_col.lower() == right_col.lower()
                    both_key_like = _likely_key_column(left_col) and _likely_key_column(right_col)
                    normalized_match = _normalize_column_name(left_col) == _normalize_column_name(right_col)
                    if same_name or both_key_like or normalized_match:
                        overlap_sql = (
                            "WITH a AS ("
                            f"SELECT DISTINCT CAST({ _quote_identifier(left_col) } AS VARCHAR) AS v "
                            f"FROM {left.quoted_table} WHERE { _quote_identifier(left_col) } IS NOT NULL LIMIT 5000"
                            "), b AS ("
                            f"SELECT DISTINCT CAST({ _quote_identifier(right_col) } AS VARCHAR) AS v "
                            f"FROM {right.quoted_table} WHERE { _quote_identifier(right_col) } IS NOT NULL LIMIT 5000"
                            ") "
                            "SELECT "
                            "(SELECT COUNT(*) FROM a INNER JOIN b USING (v)) AS overlap_count, "
                            "(SELECT COUNT(*) FROM a) AS left_distinct, "
                            "(SELECT COUNT(*) FROM b) AS right_distinct, "
                            f"(SELECT COUNT({ _quote_identifier(left_col) }) FROM {left.quoted_table}) AS left_non_null, "
                            f"(SELECT COUNT({ _quote_identifier(right_col) }) FROM {right.quoted_table}) AS right_non_null"
                        )

                        try:
                            overlap_row = conn.execute(overlap_sql).fetchone()
                            if not overlap_row:
                                continue
                            overlap_count, left_distinct, right_distinct, left_non_null, right_non_null = overlap_row
                        except Exception:
                            continue

                        left_distinct = int(left_distinct or 0)
                        right_distinct = int(right_distinct or 0)
                        overlap_count = int(overlap_count or 0)
                        left_non_null = int(left_non_null or 0)
                        right_non_null = int(right_non_null or 0)
                        denominator = min(left_distinct, right_distinct) if min(left_distinct, right_distinct) > 0 else 1
                        overlap_ratio = overlap_count / denominator
                        left_coverage = overlap_count / max(left_distinct, 1)
                        right_coverage = overlap_count / max(right_distinct, 1)

                        left_uniqueness = left_distinct / max(left_non_null, 1)
                        right_uniqueness = right_distinct / max(right_non_null, 1)
                        uniqueness_bonus = 0.1 if min(left_uniqueness, right_uniqueness) >= 0.8 else 0.0

                        if overlap_count >= 5 and overlap_ratio >= 0.2 and max(left_coverage, right_coverage) >= 0.25:
                            name_bonus = 0.15 if same_name else 0.0
                            key_bonus = 0.1 if both_key_like else 0.0
                            normalized_bonus = 0.08 if normalized_match else 0.0
                            coverage_score = min(left_coverage, right_coverage)
                            confidence = min(
                                1.0,
                                (0.55 * overlap_ratio)
                                + (0.35 * coverage_score)
                                + name_bonus
                                + key_bonus
                                + normalized_bonus
                                + uniqueness_bonus,
                            )
                            if same_name:
                                match_reason = "exact_column_name"
                            elif normalized_match:
                                match_reason = "normalized_name_match"
                            else:
                                match_reason = "key_like_overlap"
                            relationships.append(
                                RelationshipCandidate(
                                    left_table=left.table_name,
                                    left_column=left_col,
                                    right_table=right.table_name,
                                    right_column=right_col,
                                    overlap_ratio=round(overlap_ratio, 3),
                                    overlap_count=overlap_count,
                                    left_coverage=round(left_coverage, 3),
                                    right_coverage=round(right_coverage, 3),
                                    confidence=round(confidence, 3),
                                    match_reason=match_reason,
                                )
                            )

    relationships.sort(key=lambda item: item.confidence, reverse=True)
    return relationships[:30]


def _suggestions() -> list[str]:
    return [
        "Show top 10 rows from the most relevant table",
        "Find likely relationships between uploaded files",
        "Calculate total, average, and trend by month",
    ]

def _to_sentence(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return "No summary was generated."
    if cleaned[-1] not in {".", "!", "?"}:
        cleaned = f"{cleaned}."
    return cleaned

def _preview_evidence_lines(table_preview: list[dict[str, Any]], max_rows: int = 3) -> list[str]:
    lines: list[str] = []
    for row in table_preview[:max_rows]:
        if not isinstance(row, dict):
            continue
        pairs: list[str] = []
        for key, value in list(row.items())[:4]:
            pairs.append(f"{key}={value}")
        if pairs:
            lines.append("- " + ", ".join(pairs))
    return lines

def _format_complete_answer(
    answer_text: str,
    table_preview: list[dict[str, Any]] | None,
    relationship_quality: dict[str, Any] | None,
) -> str:
    text = (answer_text or "").strip()
    if not text:
        return "### Summary\n- No answer text was generated."

    # Keep model-authored markdown as-is when already structured.
    if re.search(r"(^\s*#{2,3}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|\|.+\|", text, re.MULTILINE):
        return text

    preview = table_preview or []
    rq = relationship_quality or {}
    rq_level = str(rq.get("level", "unknown")).lower()
    rq_note = str(rq.get("note", "")).strip()

    bullets: list[str] = [f"- {_to_sentence(text)}"]
    if preview:
        bullets.append(f"- **Rows returned:** {len(preview)}")
    if rq_level in {"low", "medium"} and rq_note:
        bullets.append(f"- *Join confidence: {rq_level}. {rq_note}*")

    return "\n".join(["### Summary", *bullets])


def _extract_sql(text: str) -> str:
    fenced = re.search(r"```sql\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip().rstrip(";")

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    candidate = "\n".join(lines)
    select_pos = candidate.lower().find("select")
    with_pos = candidate.lower().find("with")
    start_pos = min([pos for pos in [select_pos, with_pos] if pos >= 0], default=-1)
    if start_pos == -1:
        return ""

    return candidate[start_pos:].strip().rstrip(";")


def _build_schema_payload(
    conn: duckdb.DuckDBPyConnection,
    table_map: dict[str, str],
    relationships: list[RelationshipCandidate],
) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    for name, quoted in table_map.items():
        rows = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        tables.append(
            {
                "table": name,
                "columns": [{"name": str(row[1]), "type": str(row[2])} for row in rows],
            }
        )

    relationship_quality = _relationship_quality_summary(relationships)

    return {
        "tables": tables,
        "relationships": [
            {
                "left_table": rel.left_table,
                "left_column": rel.left_column,
                "right_table": rel.right_table,
                "right_column": rel.right_column,
                "overlap_ratio": rel.overlap_ratio,
                "overlap_count": rel.overlap_count,
                "left_coverage": rel.left_coverage,
                "right_coverage": rel.right_coverage,
                "confidence": rel.confidence,
                "match_reason": rel.match_reason,
            }
            for rel in relationships
        ],
        "relationship_quality": relationship_quality,
    }


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    fenced = re.search(r"```json\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        try:
            payload = json.loads(fenced.group(1).strip())
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        payload = json.loads(text[start : end + 1])
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None

    return None


def _configure_network_env() -> None:
    if settings.ssl_cert_file:
        os.environ["SSL_CERT_FILE"] = settings.ssl_cert_file
        os.environ["REQUESTS_CA_BUNDLE"] = settings.ssl_cert_file

    if settings.https_proxy:
        os.environ["HTTPS_PROXY"] = settings.https_proxy
        os.environ["https_proxy"] = settings.https_proxy

    if settings.adk_allow_insecure_tls:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ["CURL_CA_BUNDLE"] = ""
        os.environ["REQUESTS_CA_BUNDLE"] = ""
        ssl._create_default_https_context = ssl._create_unverified_context


def _execute_safe_sql(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    relationships: list[RelationshipCandidate] | None = None,
    min_join_confidence: float = 0.65,
    limit: int = 200,
) -> dict[str, Any]:
    if not _is_safe_sql(sql):
        return _json_safe({"ok": False, "error": "unsafe_sql"})

    if relationships is not None:
        joins_ok, join_status = _validate_join_confidence(
            sql,
            relationships,
            min_confidence=min_join_confidence,
        )
        if not joins_ok:
            return _json_safe({"ok": False, "error": join_status})

    safe_limit = max(1, min(int(limit), 500))
    wrapped_sql = f"SELECT * FROM ({sql}) AS subq LIMIT {safe_limit}"
    try:
        df = conn.execute(wrapped_sql).df()
        rows = _to_preview(df, limit=safe_limit)
        return _json_safe({
            "ok": True,
            "columns": [str(col) for col in df.columns],
            "rows": rows,
            "row_count": len(rows),
        })
    except Exception as exc:
        return _json_safe({"ok": False, "error": str(exc)})


async def _run_adk_agent_async(
    question: str,
    schema_payload: dict[str, Any],
    session_id: str,
    conn: duckdb.DuckDBPyConnection,
    table_map: dict[str, str],
    relationships: list[RelationshipCandidate],
) -> tuple[dict[str, Any] | None, str]:
    if not ADK_AVAILABLE:
        return None, "adk_unavailable"

    use_proxy_model = bool(settings.llm_proxy_base_url and settings.llm_proxy_api_key)
    if not use_proxy_model and not os.getenv("GOOGLE_API_KEY", "").strip():
        return None, "adk_no_api_key"

    _configure_network_env()

    min_join_confidence = _join_confidence_threshold(schema_payload)

    def get_schema_context() -> dict[str, Any]:
        return schema_payload

    def get_relationship_hints() -> list[dict[str, Any]]:
        return list(schema_payload.get("relationships", []))

    def execute_readonly_sql(sql: str, limit: int = 200) -> dict[str, Any]:
        return _execute_safe_sql(
            conn=conn,
            sql=sql,
            relationships=relationships,
            min_join_confidence=min_join_confidence,
            limit=limit,
        )

    def list_tables() -> list[str]:
        return sorted(table_map.keys())

    llm_model: Any = settings.google_gemini_model
    provider = "google"
    if use_proxy_model:
        try:
            openai_module = importlib.import_module("google.adk.labs.openai")
            openai_llm_class = getattr(openai_module, "OpenAILlm", None)
        except Exception:
            openai_llm_class = None

        if openai_llm_class is None or openai_async_client_cls is None:
            return None, "adk_openai_integration_missing"

        os.environ["OPENAI_BASE_URL"] = settings.llm_proxy_base_url
        os.environ["OPENAI_API_KEY"] = settings.llm_proxy_api_key

        client_kwargs: dict[str, Any] = {
            "base_url": settings.llm_proxy_base_url,
            "api_key": settings.llm_proxy_api_key,
        }
        if settings.adk_allow_insecure_tls:
            client_kwargs["http_client"] = httpx.AsyncClient(verify=False)

        openai_client = openai_async_client_cls(**client_kwargs)
        original_create = openai_client.chat.completions.create

        async def _create_with_user(*args: Any, **kwargs: Any) -> Any:
            kwargs["user"] = settings.llm_proxy_user
            return await original_create(*args, **kwargs)

        openai_client.chat.completions.create = _create_with_user  # type: ignore[method-assign]

        llm_model = openai_llm_class(model=settings.llm_proxy_model)
        llm_model._openai_client = openai_client
        provider = "openai_proxy"

    agent = LlmAgent(
        name="csv_agentic_analyst",
        model=llm_model,
        instruction=(
            "You are an agentic analytics assistant over uploaded CSV tables in DuckDB. "
            "Always call get_schema_context and get_relationship_hints first. "
            "Respect relationship confidence and coverage values from hints when joining tables. "
            "If relationship_quality is low or medium, explicitly mention uncertainty in answer_text. "
            "Only use JOIN predicates that match inferred relationship hints with confidence >= 0.65. "
            "If no suitable high-confidence join exists, prefer single-table analysis over speculative joins. "
            f"Current minimum allowed join confidence: {min_join_confidence:.2f}. "
            "If the question needs computation, call execute_readonly_sql with a SELECT/WITH query. "
            "Format answer_text as concise, display-ready Markdown. "
            "Use ## or ### headings for section titles. "
            "Use **bold** for key numbers, field names, and important terms. "
            "Use *italics* only for secondary notes or caveats. "
            "Use '-' bullets for unordered findings and '1.' numbered lists for ranked or sequential points. "
            "Keep output brief: one short heading and 2 to 5 bullets, avoid dense paragraphs. "
            "When showing comparisons or tabular content in answer_text, always use a proper Markdown table with a header separator row. "
            "Never emit raw object notation, key/value dumps, or placeholders like [object Object] in answer_text. "
            "Return final answer as JSON only with keys: answer_text, sql, table_preview, chart_spec, follow_up_suggestions. "
            "table_preview must be a clean array of objects with consistent keys across every row; keys must match intended table column names. "
            "sql must be empty string when not used. "
            "chart_spec must always follow this contract: either single-chart {chart_type: <none|pie|donut|bar|line|area|scatter|kpi>, title: string, metadata: object} "
            "or multi-chart {chart_type: <list>, title: <list>, metadata: <list>} where each index in metadata and title matches chart_type index. "
            "When visual is needed, choose chart_type(s) and provide metadata fields required by each type. "
            "For pie, metadata should be {labels: string[], data: number[]} or {slices:[{label:string,value:number}]}. "
            "For donut, use the same metadata shape as pie. "
            "For line/area/bar, metadata should be {labels: string[], data: number[], x?: string, y?: string}. "
            "For scatter, metadata should be {points:[{x:number,y:number,label?:string}], x?:string, y?:string}. "
            "When visual is not needed, set chart_type to none and metadata to {}. "
            "Never return markdown fences."
        ),
        tools=[
            FunctionTool(get_schema_context),
            FunctionTool(get_relationship_hints),
            FunctionTool(execute_readonly_sql),
            FunctionTool(list_tables),
        ],
    )

    runner = InMemoryRunner(agent=agent, app_name=settings.adk_app_name)
    session_service = InMemorySessionService()
    runner.session_service = session_service

    adk_user_id = f"user_{session_id}"
    adk_session_id = f"planner_{session_id}"
    await session_service.create_session(
        app_name=settings.adk_app_name,
        user_id=adk_user_id,
        session_id=adk_session_id,
        state={"source": "multi_csv_chat"},
    )

    message = genai_types.UserContent(parts=[genai_types.Part(text=question)])
    final_text = ""
    for attempt in range(2):
        final_text = ""
        try:
            async for runtime_event in runner.run_async(
                user_id=adk_user_id,
                session_id=adk_session_id,
                new_message=message,
            ):
                if not getattr(runtime_event, "content", None):
                    continue
                parts = getattr(runtime_event.content, "parts", None) or []
                text_parts = [part.text for part in parts if getattr(part, "text", None)]
                if text_parts:
                    final_text = "\n".join(text_parts)
            break
        except Exception as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                return None, "adk_ssl_error"
            if attempt == 1:
                return None, "adk_runtime_error"
            await asyncio.sleep(0.35)

    payload = _extract_json_payload(final_text)
    if payload is None:
        sql_fallback = _extract_sql(final_text)
        if not sql_fallback:
            return None, "adk_no_json_or_sql"

        payload = {
            "answer_text": "Generated answer using ADK planner and SQL execution.",
            "sql": sql_fallback,
            "table_preview": [],
            "chart_spec": None,
            "follow_up_suggestions": _suggestions(),
        }

    sql = str(payload.get("sql") or "").strip()
    table_preview = _normalize_table_preview(payload.get("table_preview"), max_rows=200)

    if sql and not table_preview:
        exec_result = _execute_safe_sql(
            conn=conn,
            sql=sql,
            relationships=relationships,
            min_join_confidence=min_join_confidence,
            limit=200,
        )
        if exec_result.get("ok"):
            table_preview = _normalize_table_preview(exec_result.get("rows", []), max_rows=200)
        else:
            payload["answer_text"] = (
                f"ADK produced SQL but execution failed: {exec_result.get('error', 'unknown_error')}"
            )

    normalized: dict[str, Any] = {
        "answer_text": _format_complete_answer(
            str(payload.get("answer_text") or "Generated response from ADK agent."),
            table_preview,
            schema_payload.get("relationship_quality", {}),
        ),
        "sql": sql,
        "table_preview": table_preview,
        "chart_spec": payload.get("chart_spec"),
        "follow_up_suggestions": payload.get("follow_up_suggestions")
        if isinstance(payload.get("follow_up_suggestions"), list)
        else _suggestions(),
        "relationships": [
            {
                "left_table": rel.left_table,
                "left_column": rel.left_column,
                "right_table": rel.right_table,
                "right_column": rel.right_column,
                "left_coverage": rel.left_coverage,
                "right_coverage": rel.right_coverage,
                "confidence": rel.confidence,
                "match_reason": rel.match_reason,
            }
            for rel in relationships[:12]
        ],
        "relationship_quality": schema_payload.get("relationship_quality", {}),
        "join_min_confidence": round(min_join_confidence, 2),
    }

    return normalized, f"adk_success_{provider}"


def _call_gemini_sql_planner(question: str, schema_payload: dict[str, Any]) -> tuple[str | None, str]:
    _configure_network_env()

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return None, "gemini_unavailable_no_key"

    model = settings.google_gemini_model
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )

    relationship_quality = schema_payload.get("relationship_quality", {})
    min_join_confidence = _join_confidence_threshold(schema_payload)
    prompt = (
        "You are a SQL planner for DuckDB. Generate only one SQL query for the user question. "
        "Use only provided tables and columns. Never use DML/DDL. Return SQL only, no explanation.\n\n"
        f"Join rules: use joins only when relationship hints provide confidence >= {min_join_confidence:.2f} and reasonable coverage. "
        "If high-confidence joins are unavailable, avoid JOIN and return best single-table SQL.\n\n"
        f"Question: {question}\n\n"
        f"Relationship quality summary: {json.dumps(relationship_quality)}\n\n"
        f"Schema and inferred relationships: {json.dumps(schema_payload)}"
    )

    try:
        verify: bool | str = False if settings.adk_allow_insecure_tls else True
        if settings.ssl_cert_file and not settings.adk_allow_insecure_tls:
            verify = settings.ssl_cert_file

        response = httpx.post(
            url,
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 700,
                },
            },
            timeout=20.0,
            verify=verify,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = payload.get("candidates", [])
        if not candidates:
            return None, "gemini_no_candidates"

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "\n".join(str(part.get("text", "")) for part in parts if "text" in part)
        sql = _extract_sql(text)
        if not sql:
            return None, "gemini_no_sql"
        return sql, "gemini_success"
    except Exception:
        return None, "gemini_error"


def generate_general_llm_response(session_id: str, question: str) -> dict[str, Any] | None:
    if not settings.llm_proxy_base_url or not settings.llm_proxy_api_key:
        return None

    _configure_network_env()
    url = f"{settings.llm_proxy_base_url.rstrip('/')}/chat/completions"

    try:
        _, _ = ensure_session_exists(session_id)
        manifest = read_manifest(session_id)
        table_count = len(manifest.get("files", [])) if isinstance(manifest, dict) else 0
    except Exception:
        table_count = 0

    system_prompt = (
        "You are a friendly assistant inside a CSV analytics app. "
        "Always reply conversationally like a normal LLM chat assistant. "
        "If the user asks what you can do, explain that you can analyze uploaded CSV files. "
        "If no CSVs are uploaded, politely mention they can upload CSVs for data analysis."
    )
    context_line = f"Uploaded CSV count in this session: {table_count}."

    headers = {
        "Authorization": f"Bearer {settings.llm_proxy_api_key}",
        "Content-Type": "application/json",
    }

    def _extract_content(raw_message: Any) -> str:
        if isinstance(raw_message, str):
            return raw_message.strip()
        if isinstance(raw_message, list):
            chunks: list[str] = []
            for item in raw_message:
                if isinstance(item, dict):
                    text_part = item.get("text")
                    if isinstance(text_part, str) and text_part.strip():
                        chunks.append(text_part.strip())
            return "\n".join(chunks).strip()
        return ""

    def _request_completion(max_tokens: int, extra_instruction: str | None = None) -> str | None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": context_line},
        ]
        if extra_instruction:
            messages.append({"role": "system", "content": extra_instruction})
        messages.append({"role": "user", "content": question})

        payload = {
            "model": settings.llm_proxy_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "user": settings.llm_proxy_user,
        }

        verify: bool | str = False if settings.adk_allow_insecure_tls else True
        if settings.ssl_cert_file and not settings.adk_allow_insecure_tls:
            verify = settings.ssl_cert_file

        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=25.0,
            verify=verify,
        )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices", [])
        if not choices:
            return None

        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = _extract_content(message.get("content", ""))
        return content or None

    try:
        answer_text = _request_completion(max_tokens=220)
        if not answer_text:
            answer_text = _request_completion(
                max_tokens=420,
                extra_instruction=(
                    "Respond with normal assistant text output in 1-4 sentences. "
                    "Do not leave the response blank."
                ),
            )
        if not answer_text:
            return None

        return {
            "answer_type": "text",
            "answer_text": answer_text,
            "table_preview": [],
            "chart_spec": None,
            "follow_up_suggestions": _suggestions(),
            "context_applied": False,
            "provenance": {
                "mode": "llm_general",
                "model": settings.llm_proxy_model,
                "tables_available": table_count,
            },
        }
    except Exception:
        return None


def _build_heuristic_sql(question: str, schema_payload: dict[str, Any]) -> tuple[str | None, str]:
    lower_q = question.lower()
    tables = schema_payload.get("tables", [])

    if not tables:
        return None, "heuristic_no_tables"

    if "relationship" in lower_q or "relation" in lower_q or "join" in lower_q:
        return None, "relationship_explainer"

    target_table = tables[0]
    for table in tables:
        table_name = str(table.get("table", "")).lower()
        if table_name and table_name in lower_q:
            target_table = table
            break

    table_name = str(target_table["table"])
    if "count" in lower_q or "how many" in lower_q:
        return f"SELECT COUNT(*) AS total_rows FROM {_quote_identifier(table_name)}", "heuristic_count"

    if "average" in lower_q or "avg" in lower_q:
        numeric_cols = [
            item["name"]
            for item in target_table.get("columns", [])
            if _is_numeric_type(str(item.get("type", "")))
        ]
        if numeric_cols:
            col = _quote_identifier(str(numeric_cols[0]))
            return (
                f"SELECT ROUND(AVG({col}), 2) AS average_value FROM {_quote_identifier(table_name)}",
                "heuristic_average",
            )

    return f"SELECT * FROM {_quote_identifier(table_name)} LIMIT 25", "heuristic_sample"


def _is_safe_sql(sql: str) -> bool:
    lowered = sql.strip().lower()
    if not lowered.startswith("select") and not lowered.startswith("with"):
        return False

    forbidden = [
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        "copy ",
        "attach ",
        "detach ",
        "call ",
        "pragma ",
    ]
    return not any(token in lowered for token in forbidden)


def _normalize_table_token(token: str) -> str:
    return token.strip().strip('"').strip("`").lower()


def _extract_alias_map(sql: str) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    pattern = re.compile(
        r"\b(from|join)\s+((?:\"[^\"]+\")|(?:[a-zA-Z_][\w]*))(?:\s+(?:as\s+)?([a-zA-Z_][\w]*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        raw_table = match.group(2)
        alias = match.group(3)
        table_name = _normalize_table_token(raw_table)
        alias_map[table_name] = table_name
        if alias:
            alias_map[_normalize_table_token(alias)] = table_name
    return alias_map


def _build_allowed_join_edges(
    relationships: list[RelationshipCandidate],
    min_confidence: float = 0.65,
) -> set[tuple[str, str, str, str]]:
    edges: set[tuple[str, str, str, str]] = set()
    for rel in relationships:
        if rel.confidence < min_confidence:
            continue
        left_table = rel.left_table.lower()
        right_table = rel.right_table.lower()
        left_col = rel.left_column.lower()
        right_col = rel.right_column.lower()
        edges.add((left_table, left_col, right_table, right_col))
        edges.add((right_table, right_col, left_table, left_col))
    return edges


def _validate_join_confidence(
    sql: str,
    relationships: list[RelationshipCandidate],
    min_confidence: float = 0.65,
) -> tuple[bool, str]:
    lowered = sql.lower()
    if " join " not in lowered:
        return True, "no_join"

    allowed_edges = _build_allowed_join_edges(relationships, min_confidence=min_confidence)
    if not allowed_edges:
        return False, "join_no_high_confidence_relationships"

    alias_map = _extract_alias_map(sql)
    eq_pattern = re.compile(
        r"([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\s*=\s*([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)",
        re.IGNORECASE,
    )
    eq_matches = list(eq_pattern.finditer(sql))
    if not eq_matches:
        return False, "join_without_resolvable_predicates"

    verified_any = False
    for match in eq_matches:
        left_alias = _normalize_table_token(match.group(1))
        left_col = match.group(2).lower()
        right_alias = _normalize_table_token(match.group(3))
        right_col = match.group(4).lower()

        left_table = alias_map.get(left_alias)
        right_table = alias_map.get(right_alias)
        if not left_table or not right_table:
            continue
        if left_table == right_table:
            verified_any = True
            continue

        edge = (left_table, left_col, right_table, right_col)
        if edge not in allowed_edges:
            return False, "join_not_in_inferred_relationships"
        verified_any = True

    if not verified_any:
        return False, "join_predicates_not_mapped_to_tables"
    return True, "join_validated"


def _infer_chart_spec(df: Any) -> dict[str, Any] | None:
    if df is None or df.empty or len(df.columns) < 2:
        return None

    columns = list(df.columns)
    x_col = str(columns[0])
    y_col = None

    for col in columns[1:]:
        if str(df[col].dtype).lower().startswith(("int", "float")):
            y_col = str(col)
            break

    if y_col is None:
        return None

    chart_type = "line" if _is_time_like(x_col) else "bar"
    series = _to_preview(df, limit=100)

    return {
        "chart_type": chart_type,
        "x": x_col,
        "y": y_col,
        "title": f"{y_col} by {x_col}",
        "series": series,
    }


def _relationship_answer(
    question: str,
    relationships: list[RelationshipCandidate],
    context_applied: bool,
) -> dict[str, Any]:
    rows = [
        {
            "left_table": rel.left_table,
            "left_column": rel.left_column,
            "right_table": rel.right_table,
            "right_column": rel.right_column,
            "overlap_ratio": rel.overlap_ratio,
            "confidence": rel.confidence,
            "left_coverage": rel.left_coverage,
            "right_coverage": rel.right_coverage,
            "match_reason": rel.match_reason,
        }
        for rel in relationships[:20]
    ]

    quality = _relationship_quality_summary(relationships)

    if rows:
        answer_text = (
            "Found likely relationships between uploaded CSVs using key-name and value-overlap scoring."
        )
    else:
        answer_text = (
            "No strong relationships were detected yet. Try asking with explicit table/column names or upload related keys."
        )

    answer_text = _format_complete_answer(
        f"{answer_text}\n\nRelationship quality: {quality['level']} ({quality['note']})",
        rows,
        quality,
    )

    return {
        "answer_type": "text_and_table",
        "answer_text": answer_text,
        "table_preview": rows,
        "chart_spec": None,
        "follow_up_suggestions": _suggestions(),
        "context_applied": context_applied,
        "provenance": {
            "mode": "agentic_adk_style",
            "planner": "relationship_explainer",
            "question": question,
            "relationships_considered": len(relationships),
            "relationship_quality": quality,
        },
    }


def generate_agentic_chat_response(session_id: str, question: str) -> dict[str, Any] | None:
    conn = duckdb.connect(database=":memory:")

    try:
        table_map = _register_session_tables(conn, session_id)
        if not table_map:
            return None

        context = read_chat_context(session_id)
        context_applied = bool(context.get("last_mode") == "agentic" and "now" in question.lower())

        profiles = _build_table_profiles(conn, table_map)
        relationships = _infer_relationships(conn, profiles)
        schema_payload = _build_schema_payload(conn, table_map, relationships)
        min_join_confidence = _join_confidence_threshold(schema_payload)

        adk_response: dict[str, Any] | None = None
        planned_sql: str | None = None
        planner_status = "none"
        strict_fallback_reason: str | None = None

        if settings.adk_enabled:
            try:
                adk_response, planner_status = asyncio.run(
                    _run_adk_agent_async(
                        question=question,
                        schema_payload=schema_payload,
                        session_id=session_id,
                        conn=conn,
                        table_map=table_map,
                        relationships=relationships,
                    )
                )
            except Exception:
                adk_response, planner_status = None, "adk_runner_error"

        if adk_response is not None:
            preview = adk_response.get("table_preview", [])
            chart_spec = adk_response.get("chart_spec")
            answer_type = "text_and_chart" if chart_spec else "text_and_table"
            relationship_quality = schema_payload.get("relationship_quality", {})

            answer_text = adk_response.get("answer_text") or "Generated answer using ADK agentic workflow."
            if relationship_quality.get("level") in {"low", "medium"}:
                note = relationship_quality.get("note", "")
                answer_text = f"{answer_text}\n\nData-link confidence note: {note}"
            answer_text = _format_complete_answer(answer_text, preview, relationship_quality)

            upsert_chat_context(
                session_id,
                {
                    "last_question": question,
                    "last_mode": "agentic",
                    "last_metric": "dynamic_sql",
                },
            )

            return {
                "answer_type": answer_type,
                "answer_text": answer_text,
                "table_preview": preview,
                "chart_spec": chart_spec,
                "follow_up_suggestions": adk_response.get("follow_up_suggestions", _suggestions()),
                "context_applied": context_applied,
                "provenance": {
                    "mode": "agentic_adk_full",
                    "planner": planner_status,
                    "sql": adk_response.get("sql", ""),
                    "tables_available": sorted(table_map.keys()),
                    "relationships": adk_response.get("relationships", []),
                    "relationship_quality": adk_response.get("relationship_quality", relationship_quality),
                    "join_min_confidence": adk_response.get("join_min_confidence", round(min_join_confidence, 2)),
                },
            }

        if settings.adk_strict_mode and settings.adk_enabled:
            strict_fallback_reason = planner_status

        if planned_sql is None:
            planned_sql, planner_status = _call_gemini_sql_planner(question, schema_payload)

        if planned_sql is None:
            planned_sql, planner_status = _build_heuristic_sql(question, schema_payload)

        if planner_status == "relationship_explainer":
            response = _relationship_answer(question, relationships, context_applied)
            if strict_fallback_reason:
                response["answer_text"] = (
                    "ADK response was unavailable in strict mode; returning relationship-based fallback analysis. "
                    f"ADK status: {strict_fallback_reason}.\n\n{response['answer_text']}"
                )
                response["provenance"]["mode"] = "agentic_adk_strict_fallback"
                response["provenance"]["adk_status"] = strict_fallback_reason
            upsert_chat_context(
                session_id,
                {
                    "last_question": question,
                    "last_mode": "agentic",
                    "last_metric": "relationships",
                },
            )
            return response

        if planned_sql:
            joins_ok, join_status = _validate_join_confidence(
                planned_sql,
                relationships,
                min_confidence=min_join_confidence,
            )
            if not joins_ok:
                fallback_sql, fallback_status = _build_heuristic_sql(question, schema_payload)
                if fallback_sql and _is_safe_sql(fallback_sql):
                    planned_sql = fallback_sql
                    planner_status = f"{join_status}_fallback_{fallback_status}"
                else:
                    planned_sql = None
                    planner_status = join_status

        if not planned_sql or not _is_safe_sql(planned_sql):
            if strict_fallback_reason:
                return {
                    "answer_type": "text",
                    "answer_text": (
                        "ADK response was unavailable in strict mode and no safe SQL fallback could be generated. "
                        f"ADK status: {strict_fallback_reason}. Planner status: {planner_status}. "
                        f"Current join threshold: {min_join_confidence:.2f}. "
                        "Try rephrasing with explicit high-confidence keys/metrics."
                    ),
                    "table_preview": [],
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions(),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "agentic_adk_strict_fallback",
                        "planner": planner_status,
                        "adk_status": strict_fallback_reason,
                        "join_min_confidence": round(min_join_confidence, 2),
                        "tables_available": sorted(table_map.keys()),
                    },
                }
            return None

        validated_sql = f"SELECT * FROM ({planned_sql}) AS subq LIMIT 200"
        df = conn.execute(validated_sql).df()
        preview = _to_preview(df)

        chart_spec = _infer_chart_spec(df)
        answer_type = "text_and_chart" if chart_spec else "text_and_table"

        upsert_chat_context(
            session_id,
            {
                "last_question": question,
                "last_mode": "agentic",
                "last_metric": "dynamic_sql",
            },
        )

        answer_text = (
            "Generated answer using dynamic schema-aware planning across uploaded CSVs."
            if preview
            else "Query executed but returned no rows."
        )
        if strict_fallback_reason:
            answer_text = (
                "ADK response was unavailable in strict mode; returning SQL-based fallback analysis. "
                f"ADK status: {strict_fallback_reason}.\n\n{answer_text}"
            )
        if schema_payload.get("relationship_quality", {}).get("level") in {"low", "medium"}:
            note = schema_payload.get("relationship_quality", {}).get("note", "")
            answer_text = f"{answer_text}\n\nData-link confidence note: {note}"
        answer_text = _format_complete_answer(
            answer_text,
            preview,
            schema_payload.get("relationship_quality", {}),
        )

        return {
            "answer_type": answer_type,
            "answer_text": answer_text,
            "table_preview": preview,
            "chart_spec": chart_spec,
            "follow_up_suggestions": _suggestions(),
            "context_applied": context_applied,
            "provenance": {
                "mode": "agentic_adk_strict_fallback"
                if strict_fallback_reason
                else "agentic_adk_style",
                "planner": planner_status,
                "adk_status": strict_fallback_reason,
                "join_min_confidence": round(min_join_confidence, 2),
                "sql": planned_sql,
                "tables_available": sorted(table_map.keys()),
                "relationship_quality": schema_payload.get("relationship_quality", {}),
                "relationships": [
                    {
                        "left_table": rel.left_table,
                        "left_column": rel.left_column,
                        "right_table": rel.right_table,
                        "right_column": rel.right_column,
                        "left_coverage": rel.left_coverage,
                        "right_coverage": rel.right_coverage,
                        "match_reason": rel.match_reason,
                        "confidence": rel.confidence,
                    }
                    for rel in relationships[:12]
                ],
            },
        }
    except Exception:
        return None
    finally:
        conn.close()
