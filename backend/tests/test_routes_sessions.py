from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class SessionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.settings = SimpleNamespace(
            raw_data_dir=base / "raw",
            profile_data_dir=base / "profiles",
            processed_data_dir=base / "processed",
            max_upload_files=20,
            max_file_size_mb=50,
            agentic_chat_enabled=True,
            adk_enabled=True,
            adk_strict_mode=False,
            adk_allow_insecure_tls=True,
            adk_app_name="test_app",
            google_gemini_model="gemini-test",
            llm_proxy_base_url="https://example.invalid/v1",
            llm_proxy_api_key="test-key",
            llm_proxy_model="gpt-4.1-mini",
            llm_proxy_user="tester",
            ssl_cert_file="",
            https_proxy="",
        )

        self.session_store_patch = patch("app.services.session_store.settings", self.settings)
        self.routes_patch = patch("app.api.routes_sessions.settings", self.settings)
        self.session_store_patch.start()
        self.routes_patch.start()
        self.addCleanup(self.session_store_patch.stop)
        self.addCleanup(self.routes_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_upload_and_schema_round_trip(self) -> None:
        client = TestClient(app)
        session = client.post("/api/v1/sessions").json()
        session_id = session["session_id"]

        files = [
            ("files", ("orders.csv", "order_id,total_price\n1,10\n2,20\n", "text/csv")),
            ("files", ("order_items.csv", "order_id,product_id\n1,100\n2,200\n", "text/csv")),
        ]
        upload = client.post(
            f"/api/v1/sessions/{session_id}/files",
            files=files,
            data={"clean_data": "true"},
        )

        self.assertEqual(upload.status_code, 200)
        payload = upload.json()
        self.assertEqual(len(payload["uploaded_files"]), 2)

        schema = client.get(f"/api/v1/sessions/{session_id}/schema")
        self.assertEqual(schema.status_code, 200)
        self.assertEqual(len(schema.json()["files"]), 2)

    def test_chat_prefers_llm_response_over_deterministic_fallback(self) -> None:
        client = TestClient(app)
        session_id = client.post("/api/v1/sessions").json()["session_id"]

        with patch("app.api.routes_sessions.generate_agentic_chat_response", return_value=None), patch(
            "app.api.routes_sessions.generate_general_llm_response",
            return_value={
                "answer_type": "text",
                "answer_text": "## Summary\n- LLM response used.",
                "table_preview": [],
                "chart_spec": None,
                "follow_up_suggestions": [],
                "context_applied": False,
                "provenance": {"mode": "llm_general"},
            },
        ):
            response = client.post(
                f"/api/v1/sessions/{session_id}/chat",
                json={"message": "group all the duplicate files as a pair and give me all pairs give all as a set"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["provenance"]["mode"], "llm_general")
        self.assertTrue(payload["answer_text"].startswith("## Summary"))


if __name__ == "__main__":
    unittest.main()