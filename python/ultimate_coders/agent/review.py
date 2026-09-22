"""Strict output contract for explicitly requested graph review nodes."""

from __future__ import annotations

import json

from ultimate_coders.agent.types import SubtaskReview

REVIEW_INSTRUCTIONS = """Review the supplied dependency outputs without modifying files.
Your final response must be a single JSON object, without surrounding prose:
{"approved": true, "issues": [], "suggestions": []}
Use a boolean approved and arrays of strings for issues and suggestions.
If requirements are not met, set approved=false and explain the issues.
Do not claim approval if the evidence needed to review is unavailable."""


def parse_review(summary: str) -> SubtaskReview | None:
    """Accept one complete verdict, never a JSON fragment found inside prose."""
    text = summary.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("approved"), bool):
        return None
    for key in ("issues", "suggestions"):
        if not isinstance(data.get(key), list) or any(
            not isinstance(item, str) for item in data[key]
        ):
            return None
    return SubtaskReview(
        approved=data["approved"], issues=data["issues"], suggestions=data["suggestions"]
    )
