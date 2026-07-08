from pathlib import Path
import os

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "multi-csv-chat-backend"
    app_env: str = "development"
    max_upload_files: int = 20
    max_file_size_mb: int = 50
    agentic_chat_enabled: bool = os.getenv("AGENTIC_CHAT_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    adk_enabled: bool = os.getenv("ADK_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    adk_strict_mode: bool = os.getenv("ADK_STRICT_MODE", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    adk_allow_insecure_tls: bool = os.getenv("ADK_ALLOW_INSECURE_TLS", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    adk_app_name: str = os.getenv("ADK_APP_NAME", "multi_csv_adk")
    google_gemini_model: str = os.getenv("GOOGLE_GEMINI_MODEL", "gemini-1.5-flash")
    llm_proxy_base_url: str = os.getenv("LLM_PROXY_BASE_URL", "").strip()
    llm_proxy_api_key: str = os.getenv("LLM_PROXY_API_KEY", "").strip()
    llm_proxy_model: str = os.getenv("LLM_PROXY_MODEL", "gpt-4.1")
    llm_proxy_user: str = os.getenv("LLM_PROXY_USER", "sv87799").strip() or "sv87799"
    ssl_cert_file: str = os.getenv("SSL_CERT_FILE", "").strip()
    https_proxy: str = os.getenv("HTTPS_PROXY", "").strip()

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def raw_data_dir(self) -> Path:
        return self.project_root / "data" / "raw"

    @property
    def profile_data_dir(self) -> Path:
        return self.project_root / "data" / "profiles"

    @property
    def processed_data_dir(self) -> Path:
        return self.project_root / "data" / "processed"


settings = Settings()
