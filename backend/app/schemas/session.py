from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SessionCreateResponse(BaseModel):
    session_id: str
    created_at: datetime


class UploadedFileProfile(BaseModel):
    file_name: str
    table_name: str
    rows: int
    columns: int
    profile_path: str
    cleaned: bool = False
    cleaning_summary: dict[str, Any] = Field(default_factory=dict)


class UploadFilesResponse(BaseModel):
    session_id: str
    uploaded_files: list[UploadedFileProfile]


class SchemaColumn(BaseModel):
    name: str
    inferred_dtype: str
    null_count: int
    null_pct: float
    unique_count: int
    sample_values: list[str] = Field(default_factory=list)
    numeric_stats: dict[str, Any] = Field(default_factory=dict)


class FileSchemaProfile(BaseModel):
    file_name: str
    table_name: str
    rows: int
    columns: int
    columns_profile: list[SchemaColumn]
    quality_summary: dict[str, Any]
    cleaned: bool = False
    cleaning_summary: dict[str, Any] = Field(default_factory=dict)


class SessionSchemaResponse(BaseModel):
    session_id: str
    files: list[FileSchemaProfile]


class SessionQualityResponse(BaseModel):
    session_id: str
    quality_report: dict[str, Any]
