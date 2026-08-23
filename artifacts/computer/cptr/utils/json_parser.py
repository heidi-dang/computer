"""Generous JSON parser for LLM output.

LLMs frequently return JSON wrapped in markdown fences, with trailing
commas, single quotes, or truncated output. This module provides a
best-effort parser that extracts and parses the last JSON object or
array from arbitrary text.

Usage:
    from cptr.utils.json_parser import extract_json

    result = extract_json('Here is your answer: {"title": "Hello World"}')
    # → {"title": "Hello World"}

    result = extract_json("no json here")
    # → None
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> Any | None:
    """Extract and parse the last JSON object or array from text.

    Tries increasingly aggressive strategies:
    1. Strip markdown code fences, then json.loads the whole thing
    2. Find the last {...} or [...] block via brace matching
    3. Fix common LLM mistakes (trailing commas, single quotes, etc.)
    4. Attempt to close truncated JSON

    Returns the parsed Python object, or None if nothing could be parsed.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Strip markdown code fences
    text = _strip_fences(text)

    # 1. Try parsing the whole thing directly
    parsed = _try_parse(text)
    if parsed is not None:
        return parsed

    # 2. Find the last JSON block via brace/bracket matching
    block = _find_last_json_block(text)
    if block:
        parsed = _try_parse(block)
        if parsed is not None:
            return parsed

        # 3. Try fixing common issues
        fixed = _fix_json(block)
        parsed = _try_parse(fixed)
        if parsed is not None:
            return parsed

        # 4. Try closing truncated JSON
        closed = _close_json(fixed)
        parsed = _try_parse(closed)
        if parsed is not None:
            return parsed

    # 5. Last resort: try fixing the whole text
    fixed = _fix_json(text)
    parsed = _try_parse(fixed)
    if parsed is not None:
        return parsed

    closed = _close_json(fixed)
    return _try_parse(closed)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` etc.)."""
    text = text.strip()
    # Match opening fence with optional language tag
    if text.startswith("```"):
        # Remove opening fence line
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        # Remove closing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _try_parse(text: str) -> Any | None:
    """Try json.loads, return None on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _find_last_json_block(text: str) -> str | None:
    """Find the last top-level {...} or [...] block using brace matching.

    Respects string quoting so braces inside strings are ignored.
    """
    # Find the last opening brace/bracket
    for opener, closer in [("{", "}"), ("[", "]")]:
        # Search backwards for the last opener
        start = _find_last_opener(text, opener, closer)
        if start is not None:
            end = _find_matching_close(text, start, opener, closer)
            if end is not None:
                return text[start : end + 1]
    return None


def _find_last_opener(text: str, opener: str, closer: str) -> int | None:
    """Find the position of the last top-level opener."""
    last = None
    i = len(text) - 1
    depth = 0
    in_string = False
    escape = False

    while i >= 0:
        ch = text[i]
        if escape:
            escape = False
            i -= 1
            continue
        # Check for escape (looking backwards is tricky, just do forward scan)
        # Fall back to simpler approach: find all openers via rfind
        break

    # Simpler: scan forward, track the last top-level opener
    in_string = False
    escape = False
    depth = 0
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            if depth == 0:
                last = i
            depth += 1
        elif ch == closer:
            depth -= 1
    return last


def _find_matching_close(text: str, start: int, opener: str, closer: str) -> int | None:
    """From position `start`, find the matching closing brace/bracket."""
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
    # Unmatched — return the last position (for truncated JSON)
    return None


def _fix_json(text: str) -> str:
    """Fix common LLM JSON mistakes."""
    s = text.strip()

    # Replace single quotes with double quotes (but not within double-quoted strings)
    # Only do this if there are no double quotes (pure single-quote JSON)
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')

    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # Fix unquoted keys: word: → "word":
    s = re.sub(r"(?<=[{,])\s*(\w+)\s*:", r' "\1":', s)

    # Remove JavaScript-style comments
    s = re.sub(r"//[^\n]*", "", s)

    return s


def _close_json(text: str) -> str:
    """Try to close truncated JSON by adding missing brackets/braces."""
    s = text.strip()
    if not s:
        return s

    # Count unmatched openers
    stack: list[str] = []
    in_string = False
    escape = False

    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()

    # Close any open string
    if in_string:
        s += '"'

    # Remove any trailing comma before we close
    s = re.sub(r",\s*$", "", s)

    # Append missing closers in reverse order
    while stack:
        s += stack.pop()

    return s
