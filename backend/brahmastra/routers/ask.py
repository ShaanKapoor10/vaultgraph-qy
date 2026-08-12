"""
Ask router — GraphRAG natural-language question answering.

POST /ask  { "question": "...", "mode": "auto" | "local" | "global" }
  → { "mode", "answer", "entities", "citations" }
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/ask", tags=["ask"])


class AskRequest(BaseModel):
    question: str
    mode: Literal["auto", "local", "global"] = "auto"
    # Hops to traverse for local search. Omit to let the router pick: 2 for
    # chained questions ("Sarah's manager's other reports"), 1 otherwise.
    depth: int | None = Field(default=None, ge=1, le=3)


@router.post("")
async def ask(body: AskRequest) -> dict[str, Any]:
    from brahmastra.rag import answer_question
    return answer_question(body.question, mode=body.mode, depth=body.depth)
