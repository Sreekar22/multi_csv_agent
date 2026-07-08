from __future__ import annotations

import re
from typing import Any

import duckdb

from app.core.config import settings
from app.services.session_store import (
    ensure_session_exists,
    read_chat_context,
    read_manifest,
    upsert_chat_context,
)


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


def _get_columns(conn: duckdb.DuckDBPyConnection, quoted_table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    return {str(row[1]).lower() for row in rows}


def _to_preview(df: Any, limit: int = 20) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.head(limit).to_dict(orient="records")


def _is_follow_up(question: str) -> bool:
    lower_q = question.lower()
    markers = [
        "now",
        "also",
        "break it down",
        "drill down",
        "same",
        "that",
        "this",
        "continue",
    ]
    return any(marker in lower_q for marker in markers)


def _suggestions(metric: str | None) -> list[str]:
    if metric == "revenue":
        return [
            "Now break it down by category",
            "Show revenue trend over last 12 months",
            "Show top products by units sold",
        ]
    if metric == "orders":
        return [
            "Show orders trend over last 12 months",
            "Now break it down by category",
            "What is average order value?",
        ]
    if metric == "payments":
        return [
            "Now break it down by status",
            "Break payments down by method",
            "Show total payment amount",
        ]
    if metric == "shipments":
        return [
            "Show shipment status breakdown",
            "How many shipments are delayed?",
            "Show shipment status trend",
        ]
    if metric == "reviews":
        return [
            "What is average rating?",
            "Show rating distribution",
            "Show top rated products",
        ]
    return [
        "What is the total revenue?",
        "Show revenue trend over last 6 months",
        "Show top products",
    ]


def _fallback_response(question: str, tables: list[str], context_applied: bool) -> dict[str, Any]:
    return {
        "answer_type": "text",
        "answer_text": (
            "I could not map that question to a supported deterministic query yet. "
            "Try asking: total revenue, total orders, average order value, top products, "
            "revenue trend for last 6 months, orders trend over time, payment breakdowns, "
            "shipment status, or average rating."
        ),
        "table_preview": [],
        "chart_spec": None,
        "follow_up_suggestions": _suggestions(None),
        "context_applied": context_applied,
        "provenance": {
            "mode": "deterministic_v2_context",
            "tables_available": tables,
            "question": question,
        },
    }


def _persist_context(session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    return upsert_chat_context(session_id, updates)


def generate_chat_response(session_id: str, question: str) -> dict[str, Any]:
    conn = duckdb.connect(database=":memory:")

    try:
        table_map = _register_session_tables(conn, session_id)
        tables_available = sorted(table_map.keys())

        if not tables_available:
            return {
                "answer_type": "text",
                "answer_text": "No uploaded CSVs found for this session.",
                "table_preview": [],
                "chart_spec": None,
                "follow_up_suggestions": _suggestions(None),
                "context_applied": False,
                "provenance": {"mode": "deterministic_v2_context", "tables_available": []},
            }

        lower_q = question.lower()
        context = read_chat_context(session_id)
        is_follow_up = _is_follow_up(question)
        context_applied = False

        asks_revenue = "revenue" in lower_q or "sales" in lower_q
        asks_trend = "trend" in lower_q or "over time" in lower_q or "monthly" in lower_q
        asks_by_category = "by category" in lower_q or "category-wise" in lower_q
        asks_by_month = "by month" in lower_q
        asks_orders = "order" in lower_q and not asks_revenue
        asks_payment = "payment" in lower_q
        asks_shipment = "shipment" in lower_q or "delivery" in lower_q
        asks_review = "review" in lower_q or "rating" in lower_q
        asks_by_status = "status" in lower_q
        asks_by_method = "method" in lower_q
        asks_average = "average" in lower_q or "avg" in lower_q
        asks_distribution = "distribution" in lower_q or "breakdown" in lower_q
        asks_last_6_months = "6 month" in lower_q or "last 6" in lower_q
        asks_last_year = (
            "past year" in lower_q
            or "last year" in lower_q
            or "last 12 months" in lower_q
            or "12 months" in lower_q
        )

        if is_follow_up and not asks_revenue and context.get("last_metric") == "revenue":
            asks_revenue = True
            context_applied = True

        if is_follow_up and not asks_orders and context.get("last_metric") == "orders":
            asks_orders = True
            context_applied = True

        if is_follow_up and not asks_payment and context.get("last_metric") == "payments":
            asks_payment = True
            context_applied = True

        if is_follow_up and not asks_shipment and context.get("last_metric") == "shipments":
            asks_shipment = True
            context_applied = True

        if is_follow_up and not asks_review and context.get("last_metric") == "reviews":
            asks_review = True
            context_applied = True

        if is_follow_up and not asks_trend and (
            context.get("last_view") == "trend" or asks_by_month
        ):
            asks_trend = True
            context_applied = True

        if is_follow_up and not asks_last_6_months and not asks_last_year:
            if context.get("last_time_window") == "6m":
                asks_last_6_months = True
                context_applied = True
            elif context.get("last_time_window") == "12m":
                asks_last_year = True
                context_applied = True

        has_orders = "orders" in table_map
        has_order_items = "order_items" in table_map
        has_products = "products" in table_map
        has_payment = "payment" in table_map
        has_shipments = "shipments" in table_map
        has_reviews = "reviews" in table_map

        orders_cols = _get_columns(conn, table_map["orders"]) if has_orders else set()
        order_items_cols = (
            _get_columns(conn, table_map["order_items"]) if has_order_items else set()
        )
        products_cols = _get_columns(conn, table_map["products"]) if has_products else set()
        payment_cols = _get_columns(conn, table_map["payment"]) if has_payment else set()
        shipment_cols = _get_columns(conn, table_map["shipments"]) if has_shipments else set()
        review_cols = _get_columns(conn, table_map["reviews"]) if has_reviews else set()

        if asks_payment:
            if has_payment and asks_by_status and "transaction_status" in payment_cols and "amount" in payment_cols:
                sql = (
                    f"SELECT transaction_status, ROUND(SUM(amount), 2) AS total_amount "
                    f"FROM {table_map['payment']} GROUP BY 1 ORDER BY total_amount DESC"
                )
                df = conn.execute(sql).df()
                preview = _to_preview(df)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "payments",
                        "last_view": "breakdown",
                        "last_dimension": "transaction_status",
                    },
                )

                return {
                    "answer_type": "text_and_chart",
                    "answer_text": "Payment amount breakdown by transaction status.",
                    "table_preview": preview,
                    "chart_spec": {
                        "chart_type": "bar",
                        "x": "transaction_status",
                        "y": "total_amount",
                        "title": "Payments by Status",
                        "series": preview,
                    },
                    "follow_up_suggestions": _suggestions("payments"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["payment"],
                    },
                }

            if has_payment and asks_by_method and "payment_method" in payment_cols and "amount" in payment_cols:
                sql = (
                    f"SELECT payment_method, ROUND(SUM(amount), 2) AS total_amount "
                    f"FROM {table_map['payment']} GROUP BY 1 ORDER BY total_amount DESC"
                )
                df = conn.execute(sql).df()
                preview = _to_preview(df)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "payments",
                        "last_view": "breakdown",
                        "last_dimension": "payment_method",
                    },
                )

                return {
                    "answer_type": "text_and_chart",
                    "answer_text": "Payment amount breakdown by payment method.",
                    "table_preview": preview,
                    "chart_spec": {
                        "chart_type": "bar",
                        "x": "payment_method",
                        "y": "total_amount",
                        "title": "Payments by Method",
                        "series": preview,
                    },
                    "follow_up_suggestions": _suggestions("payments"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["payment"],
                    },
                }

            if has_payment and "amount" in payment_cols:
                sql = f"SELECT ROUND(SUM(amount), 2) AS total_payment_amount FROM {table_map['payment']}"
                df = conn.execute(sql).df()
                value = float(df.iloc[0]["total_payment_amount"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "payments",
                        "last_view": "total",
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": f"Total payment amount is {value:,.2f}.",
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("payments"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["payment"],
                    },
                }

        if asks_shipment:
            if has_shipments and (asks_by_status or asks_distribution) and "shipment_status" in shipment_cols:
                sql = (
                    f"SELECT shipment_status, COUNT(*) AS shipments "
                    f"FROM {table_map['shipments']} GROUP BY 1 ORDER BY shipments DESC"
                )
                df = conn.execute(sql).df()
                preview = _to_preview(df)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "shipments",
                        "last_view": "breakdown",
                        "last_dimension": "shipment_status",
                    },
                )

                return {
                    "answer_type": "text_and_chart",
                    "answer_text": "Shipment count breakdown by shipment status.",
                    "table_preview": preview,
                    "chart_spec": {
                        "chart_type": "bar",
                        "x": "shipment_status",
                        "y": "shipments",
                        "title": "Shipments by Status",
                        "series": preview,
                    },
                    "follow_up_suggestions": _suggestions("shipments"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["shipments"],
                    },
                }

            if has_shipments and "shipment_status" in shipment_cols:
                delayed_sql = (
                    f"SELECT COUNT(*) AS delayed_shipments "
                    f"FROM {table_map['shipments']} "
                    f"WHERE LOWER(shipment_status) LIKE '%delay%'"
                )
                df = conn.execute(delayed_sql).df()
                delayed = int(df.iloc[0]["delayed_shipments"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "shipments",
                        "last_view": "total",
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": f"Detected {delayed:,} delayed shipments.",
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("shipments"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": delayed_sql,
                        "tables_used": ["shipments"],
                    },
                }

        if asks_review:
            if has_reviews and asks_average and "rating" in review_cols:
                sql = f"SELECT ROUND(AVG(rating), 2) AS average_rating FROM {table_map['reviews']}"
                df = conn.execute(sql).df()
                value = float(df.iloc[0]["average_rating"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "reviews",
                        "last_view": "average",
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": f"Average rating is {value:,.2f}.",
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("reviews"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["reviews"],
                    },
                }

            if has_reviews and (asks_distribution or asks_by_status or asks_by_category) and "rating" in review_cols:
                sql = (
                    f"SELECT rating, COUNT(*) AS review_count "
                    f"FROM {table_map['reviews']} GROUP BY 1 ORDER BY 1"
                )
                df = conn.execute(sql).df()
                preview = _to_preview(df)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "reviews",
                        "last_view": "distribution",
                        "last_dimension": "rating",
                    },
                )

                return {
                    "answer_type": "text_and_chart",
                    "answer_text": "Rating distribution across reviews.",
                    "table_preview": preview,
                    "chart_spec": {
                        "chart_type": "bar",
                        "x": "rating",
                        "y": "review_count",
                        "title": "Review Rating Distribution",
                        "series": preview,
                    },
                    "follow_up_suggestions": _suggestions("reviews"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["reviews"],
                    },
                }

        if (
            asks_trend
            and asks_revenue
            and has_orders
            and has_order_items
            and {"order_id", "order_date"}.issubset(orders_cols)
            and {"order_id", "quantity", "price_at_purchase"}.issubset(order_items_cols)
        ):
            interval_sql = ""
            title_suffix = "All Available Months"
            answer_window = "the available data range"
            time_window = "all"

            if asks_last_6_months:
                interval_sql = "WHERE CAST(o.order_date AS DATE) >= current_date - INTERVAL '6 months' "
                title_suffix = "Last 6 Months"
                answer_window = "the last 6 months"
                time_window = "6m"
            elif asks_last_year:
                interval_sql = "WHERE CAST(o.order_date AS DATE) >= current_date - INTERVAL '12 months' "
                title_suffix = "Last 12 Months"
                answer_window = "the last 12 months"
                time_window = "12m"

            sql = (
                f"SELECT strftime(date_trunc('month', CAST(o.order_date AS DATE)), '%Y-%m') AS month, "
                f"ROUND(SUM(oi.quantity * oi.price_at_purchase), 2) AS revenue "
                f"FROM {table_map['orders']} o "
                f"JOIN {table_map['order_items']} oi ON o.order_id = oi.order_id "
                f"{interval_sql}"
                f"GROUP BY 1 ORDER BY 1"
            )
            df = conn.execute(sql).df()
            preview = _to_preview(df)

            _persist_context(
                session_id,
                {
                    "last_question": question,
                    "last_metric": "revenue",
                    "last_view": "trend",
                    "last_time_window": time_window,
                    "last_dimension": "month",
                },
            )

            return {
                "answer_type": "text_and_chart",
                "answer_text": (
                    f"Computed monthly revenue trend for {answer_window}."
                    if preview
                    else f"No rows found for {answer_window}."
                ),
                "table_preview": preview,
                "chart_spec": {
                    "chart_type": "line",
                    "x": "month",
                    "y": "revenue",
                    "title": f"Revenue Trend ({title_suffix})",
                    "series": preview,
                },
                "follow_up_suggestions": _suggestions("revenue"),
                "context_applied": context_applied,
                "provenance": {
                    "mode": "deterministic_v2_context",
                    "sql": sql,
                    "tables_used": ["orders", "order_items"],
                },
            }

        if asks_revenue and asks_by_category:
            if (
                has_order_items
                and has_products
                and {"product_id", "quantity", "price_at_purchase"}.issubset(order_items_cols)
                and {"product_id", "category"}.issubset(products_cols)
            ):
                sql = (
                    f"SELECT p.category, ROUND(SUM(oi.quantity * oi.price_at_purchase), 2) AS revenue "
                    f"FROM {table_map['order_items']} oi "
                    f"JOIN {table_map['products']} p ON oi.product_id = p.product_id "
                    f"GROUP BY 1 ORDER BY revenue DESC"
                )
                df = conn.execute(sql).df()
                preview = _to_preview(df)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "revenue",
                        "last_view": "breakdown",
                        "last_time_window": context.get("last_time_window", "all"),
                        "last_dimension": "category",
                    },
                )

                return {
                    "answer_type": "text_and_chart",
                    "answer_text": "Revenue breakdown by category.",
                    "table_preview": preview,
                    "chart_spec": {
                        "chart_type": "bar",
                        "x": "category",
                        "y": "revenue",
                        "title": "Revenue by Category",
                        "series": preview,
                    },
                    "follow_up_suggestions": _suggestions("revenue"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["order_items", "products"],
                    },
                }

        if (
            asks_trend
            and asks_orders
            and has_orders
            and {"order_id", "order_date"}.issubset(orders_cols)
        ):
            interval_sql = ""
            title_suffix = "All Available Months"
            answer_window = "the available data range"
            time_window = "all"

            if asks_last_6_months:
                interval_sql = "WHERE CAST(order_date AS DATE) >= current_date - INTERVAL '6 months' "
                title_suffix = "Last 6 Months"
                answer_window = "the last 6 months"
                time_window = "6m"
            elif asks_last_year:
                interval_sql = "WHERE CAST(order_date AS DATE) >= current_date - INTERVAL '12 months' "
                title_suffix = "Last 12 Months"
                answer_window = "the last 12 months"
                time_window = "12m"

            sql = (
                f"SELECT strftime(date_trunc('month', CAST(order_date AS DATE)), '%Y-%m') AS month, "
                f"COUNT(DISTINCT order_id) AS total_orders "
                f"FROM {table_map['orders']} "
                f"{interval_sql}"
                f"GROUP BY 1 ORDER BY 1"
            )
            df = conn.execute(sql).df()
            preview = _to_preview(df)

            _persist_context(
                session_id,
                {
                    "last_question": question,
                    "last_metric": "orders",
                    "last_view": "trend",
                    "last_time_window": time_window,
                    "last_dimension": "month",
                },
            )

            return {
                "answer_type": "text_and_chart",
                "answer_text": (
                    f"Computed monthly orders trend for {answer_window}."
                    if preview
                    else f"No rows found for {answer_window}."
                ),
                "table_preview": preview,
                "chart_spec": {
                    "chart_type": "line",
                    "x": "month",
                    "y": "total_orders",
                    "title": f"Orders Trend ({title_suffix})",
                    "series": preview,
                },
                "follow_up_suggestions": _suggestions("orders"),
                "context_applied": context_applied,
                "provenance": {
                    "mode": "deterministic_v2_context",
                    "sql": sql,
                    "tables_used": ["orders"],
                },
            }

        if asks_orders and asks_by_category:
            if (
                has_orders
                and has_order_items
                and has_products
                and {"order_id"}.issubset(orders_cols)
                and {"order_id", "product_id"}.issubset(order_items_cols)
                and {"product_id", "category"}.issubset(products_cols)
            ):
                sql = (
                    f"SELECT p.category, COUNT(DISTINCT o.order_id) AS total_orders "
                    f"FROM {table_map['orders']} o "
                    f"JOIN {table_map['order_items']} oi ON o.order_id = oi.order_id "
                    f"JOIN {table_map['products']} p ON oi.product_id = p.product_id "
                    f"GROUP BY 1 ORDER BY total_orders DESC"
                )
                df = conn.execute(sql).df()
                preview = _to_preview(df)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "orders",
                        "last_view": "breakdown",
                        "last_time_window": context.get("last_time_window", "all"),
                        "last_dimension": "category",
                    },
                )

                return {
                    "answer_type": "text_and_chart",
                    "answer_text": "Order count breakdown by category.",
                    "table_preview": preview,
                    "chart_spec": {
                        "chart_type": "bar",
                        "x": "category",
                        "y": "total_orders",
                        "title": "Orders by Category",
                        "series": preview,
                    },
                    "follow_up_suggestions": _suggestions("orders"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["orders", "order_items", "products"],
                    },
                }

        if "total revenue" in lower_q or (asks_revenue and not asks_trend):
            if has_order_items and {"quantity", "price_at_purchase"}.issubset(order_items_cols):
                sql = f"SELECT ROUND(SUM(quantity * price_at_purchase), 2) AS total_revenue FROM {table_map['order_items']}"
                df = conn.execute(sql).df()
                value = float(df.iloc[0]["total_revenue"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "revenue",
                        "last_view": "total",
                        "last_time_window": "all",
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": f"Total revenue is {value:,.2f}.",
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("revenue"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["order_items"],
                    },
                }

            if has_orders and "total_price" in orders_cols:
                where_clause = ""
                time_window = "all"
                if asks_last_year and "order_date" in orders_cols:
                    where_clause = " WHERE CAST(order_date AS DATE) >= current_date - INTERVAL '12 months'"
                    time_window = "12m"

                sql = (
                    f"SELECT ROUND(SUM(total_price), 2) AS total_revenue "
                    f"FROM {table_map['orders']}{where_clause}"
                )
                df = conn.execute(sql).df()
                value = float(df.iloc[0]["total_revenue"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "revenue",
                        "last_view": "total",
                        "last_time_window": time_window,
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": (
                        f"Total revenue for the last 12 months is {value:,.2f}."
                        if asks_last_year
                        else f"Total revenue is {value:,.2f}."
                    ),
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("revenue"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["orders"],
                    },
                }

        if "total orders" in lower_q or "number of orders" in lower_q:
            if has_orders and "order_id" in orders_cols:
                where_clause = ""
                time_window = "all"
                if asks_last_year and "order_date" in orders_cols:
                    where_clause = " WHERE CAST(order_date AS DATE) >= current_date - INTERVAL '12 months'"
                    time_window = "12m"
                sql = (
                    f"SELECT COUNT(DISTINCT order_id) AS total_orders "
                    f"FROM {table_map['orders']}{where_clause}"
                )
                df = conn.execute(sql).df()
                value = int(df.iloc[0]["total_orders"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "orders",
                        "last_view": "total",
                        "last_time_window": time_window,
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": (
                        f"Total orders in the last 12 months are {value:,}."
                        if asks_last_year
                        else f"Total orders are {value:,}."
                    ),
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("orders"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["orders"],
                    },
                }

        if "average order value" in lower_q or "aov" in lower_q:
            if has_orders and "total_price" in orders_cols:
                sql = f"SELECT ROUND(AVG(total_price), 2) AS average_order_value FROM {table_map['orders']}"
                df = conn.execute(sql).df()
                value = float(df.iloc[0]["average_order_value"] or 0)

                _persist_context(
                    session_id,
                    {
                        "last_question": question,
                        "last_metric": "orders",
                        "last_view": "average",
                        "last_time_window": "all",
                        "last_dimension": None,
                    },
                )

                return {
                    "answer_type": "text_and_table",
                    "answer_text": f"Average order value is {value:,.2f}.",
                    "table_preview": _to_preview(df),
                    "chart_spec": None,
                    "follow_up_suggestions": _suggestions("orders"),
                    "context_applied": context_applied,
                    "provenance": {
                        "mode": "deterministic_v2_context",
                        "sql": sql,
                        "tables_used": ["orders"],
                    },
                }

        if (
            "top" in lower_q
            and "product" in lower_q
            and has_order_items
            and has_products
            and {"product_id", "quantity"}.issubset(order_items_cols)
            and {"product_id", "product_name"}.issubset(products_cols)
        ):
            sql = (
                f"SELECT p.product_name, SUM(oi.quantity) AS units_sold "
                f"FROM {table_map['order_items']} oi "
                f"JOIN {table_map['products']} p ON oi.product_id = p.product_id "
                f"GROUP BY 1 ORDER BY units_sold DESC LIMIT 10"
            )
            df = conn.execute(sql).df()
            preview = _to_preview(df)

            _persist_context(
                session_id,
                {
                    "last_question": question,
                    "last_metric": "revenue",
                    "last_view": "ranking",
                    "last_time_window": context.get("last_time_window", "all"),
                    "last_dimension": "product",
                },
            )

            return {
                "answer_type": "text_and_table",
                "answer_text": "Top 10 products by units sold.",
                "table_preview": preview,
                "chart_spec": {
                    "chart_type": "bar",
                    "x": "product_name",
                    "y": "units_sold",
                    "title": "Top Products by Units Sold",
                    "series": preview,
                },
                "follow_up_suggestions": _suggestions("revenue"),
                "context_applied": context_applied,
                "provenance": {
                    "mode": "deterministic_v2_context",
                    "sql": sql,
                    "tables_used": ["order_items", "products"],
                },
            }

        fallback = _fallback_response(question, tables_available, context_applied)
        _persist_context(session_id, {"last_question": question})
        return fallback
    finally:
        conn.close()
