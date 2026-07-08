from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=2, max_length=2000)


class ChatResponse(BaseModel):
    session_id: str
    question: str
    answer_type: Literal["text", "table", "text_and_table", "text_and_chart"]
    answer_text: str
    table_preview: list[dict[str, Any]] = Field(default_factory=list)
    chart_spec: dict[str, Any] | None = None
    follow_up_suggestions: list[str] = Field(default_factory=list)
    context_applied: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)
