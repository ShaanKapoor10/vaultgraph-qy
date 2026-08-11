"""
Ask router — GraphRAG natural-language question answering.

POST /ask  { "question": "...", "mode": "auto" | "local" | "global" }
  → { "mode", "answer", "entities", "citations" }
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/ask", tags=["ask"])


class AskRequest(BaseModel):
    question: str
    mode: Literal["auto", "local", "global"] = "auto"


@router.post("")
async def ask(body: AskRequest) -> dict[str, Any]:
    from brahmastra.rag import answer_question
    return answer_question(body.question, mode=body.mode)
