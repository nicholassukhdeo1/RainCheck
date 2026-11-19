"""Classify Yahoo listing titles as one of a brand's tracked items, or none.

This is a constrained classifier, not an agent: no tools, no actions.  The model can only
return an item ID from the list it was given (or null), and Python validates every ID again,
so a confused or manipulated model can at worst produce a wrong label.

The contract with callers is "a result, never an exception" for anything the network or the
model does; only caller bugs (bad arguments) raise ValueError.
"""
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import llm_client

logger = logging.getLogger(__name__)

# More titles per request means longer responses and more risk that one bad answer
# fails the whole batch; 25 keeps a response well inside MAX_TOKENS.
MAX_TITLES_PER_REQUEST = 25
# A hard limit on thinking plus reply, and so also the basis of the worst-case cost estimate.
MAX_TOKENS = 6000
EVIDENCE_MAX_CHARS = 200
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT_VERSION = "listing_match_v1"
CONFIDENCE_LEVELS = ("high", "medium", "low")

_VERSION_PATTERN = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class Prompt:
    version: str  # file stem, for example "listing_match_v1"
    text: str     # the instructions, header removed, stripped
    sha256: str   # hex SHA-256 of the whole file's bytes


@dataclass(frozen=True)
class Decision:
    index: int
    item_id: int | None
    confidence: str  # one of CONFIDENCE_LEVELS, lowercase
    evidence: str    # at most EVIDENCE_MAX_CHARS characters


@dataclass(frozen=True)
class BatchResult:
    decisions: tuple[Decision, ...] | None  # None exactly when failure is set
    failure: str | None                      # a reason code
    input_tokens: int
    output_tokens: int


class PromptNotReadyError(ValueError):
    """The prompt file exists but has only its contract header."""


def load_prompt(version: str = DEFAULT_PROMPT_VERSION) -> Prompt:
    # fullmatch (not `$`) so a trailing newline cannot slip through; a version is never a path.
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("prompt version must match [a-z0-9_]+")
    raw = (PROMPT_DIR / f"{version}.md").read_bytes()
    text = raw.decode("utf-8")

    if text.startswith("<!--"):
        end = text.find("-->")
        if end == -1:
            raise ValueError(f"src/prompts/{version}.md has an unterminated header comment.")
        text = text[end + len("-->"):]
    instructions = text.strip()
    if not instructions:
        raise PromptNotReadyError(f"src/prompts/{version}.md has no instructions yet.")
    # Hashing the whole file means any edit, even to the header, changes the hash.
    return Prompt(version=version, text=instructions, sha256=hashlib.sha256(raw).hexdigest())


def _sorted_items(items):
    return sorted(items, key=lambda item: item.id)


def _schema(items):
    # `enum` with the provided IDs is the grammar-level guarantee that the model can only
    # pick from the list.  Index range and count cannot be expressed, so Python checks them.
    item_ids = [item.id for item in _sorted_items(items)] + [None]
    return {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "item_id": {"enum": item_ids},
                        "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
                        # Named "evidence", not "reason": a field asking for reasoning can
                        # trigger a refusal.
                        "evidence": {"type": "string"},
                    },
                    "required": ["index", "item_id", "confidence", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["classifications"],
        "additionalProperties": False,
    }


def build_request(brand: str, titles: Sequence[str], items: Sequence, prompt: Prompt,
                  *, model: str, effort: str) -> dict:
    """The exact keyword arguments for client.messages.create.  Pure: no I/O."""
    # Sorted items and keywords make the same item set produce the same payload and schema.
    payload = {
        "brand": brand,
        "items": [
            {
                "item_id": item.id,
                "name": item.name,
                "include_keywords": sorted(item.keywords),
                "exclude_keywords": sorted(item.exclude_keywords),
            }
            for item in _sorted_items(items)
        ],
        "titles": [{"index": i, "title": title} for i, title in enumerate(titles)],
    }
    # Titles live only here, as JSON string values, never in `system`: JSON escaping stops a
    # title from closing its string or adding keys.  ensure_ascii=False keeps Japanese readable.
    payload_json = json.dumps(payload, ensure_ascii=False)
    # No thinking, tools, tool_choice, temperature, stop_sequences, or assistant prefill:
    # Opus 5.5 rejects some of them and prefill is incompatible with JSON outputs.
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": prompt.text,
        "messages": [{"role": "user", "content": payload_json}],
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": _schema(items)},
        },
    }


def _is_int(value):
    # bool is an int subclass; True must not pass as index 1 or item 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _check_inputs(titles, items):
    if not 1 <= len(titles) <= MAX_TITLES_PER_REQUEST:
        raise ValueError(f"titles must hold 1 to {MAX_TITLES_PER_REQUEST} entries")
    for title in titles:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("every title must be a non-blank string")
    if not items:
        raise ValueError("items must not be empty")
    ids = [item.id for item in items]
    if any(not _is_int(item_id) or item_id <= 0 for item_id in ids):
        raise ValueError("item IDs must be positive integers")
    if len(set(ids)) != len(ids):
        raise ValueError("item IDs must be unique")


def _fail(reason, count, input_tokens=0, output_tokens=0, detail=None):
    # One warning with only a reason code, an exception class name, and a count: never titles,
    # evidence, response text, or credentials (RC-10 logging rules).
    label = reason if detail is None else f"{reason}: {detail}"
    logger.warning("LLM classification failed (%s): %d titles not classified", label, count)
    return BatchResult(None, reason, input_tokens, output_tokens)


def classify_titles(client, brand: str, titles: Sequence[str], items: Sequence, prompt: Prompt,
                    *, model: str, effort: str) -> BatchResult:
    _check_inputs(titles, items)
    count = len(titles)
    request = build_request(brand, titles, items, prompt, model=model, effort=effort)

    try:
        response = client.messages.create(**request)
    except Exception as exc:  # noqa: BLE001 - deliberate: this is the one network boundary
        # The caller's contract is "a result, never an exception", so a daily job is protected.
        return _fail("request_error", count, detail=type(exc).__name__)

    # Tokens are billed even for refusals and truncations, so read them before validating.
    input_tokens, output_tokens = llm_client.usage_tokens(response)

    def fail(reason):
        return _fail(reason, count, input_tokens, output_tokens)

    text, reason = llm_client.response_text(response)
    if text is None:
        return fail(reason)
    try:
        document = json.loads(text)
    except ValueError:
        return fail("invalid_json")

    # Each stage checks every element before the next stage runs, so the first failing check
    # in the documented order wins.  A wrong answer about one title taints the whole batch.
    if not isinstance(document, dict) or set(document) != {"classifications"}:
        return fail("invalid_shape")
    rows = document["classifications"]
    if not isinstance(rows, list):
        return fail("invalid_shape")
    keys = {"index", "item_id", "confidence", "evidence"}
    if any(not isinstance(row, dict) or set(row) != keys for row in rows):
        return fail("invalid_shape")
    if len(rows) != count:
        return fail("wrong_count")
    if any(not _is_int(row["index"]) or not 0 <= row["index"] < count for row in rows):
        return fail("bad_index")
    if len({row["index"] for row in rows}) != count:
        return fail("duplicate_index")
    allowed_ids = {item.id for item in items}
    if any(row["item_id"] is not None and (not _is_int(row["item_id"]) or row["item_id"] not in allowed_ids)
           for row in rows):
        return fail("unknown_item")
    # Enum capitalization is not guaranteed by the API, so compare case-insensitively.
    if any(not isinstance(row["confidence"], str) or row["confidence"].lower() not in CONFIDENCE_LEVELS
           for row in rows):
        return fail("bad_confidence")
    if any(not isinstance(row["evidence"], str) for row in rows):
        return fail("invalid_shape")

    decisions = tuple(
        Decision(
            index=row["index"],
            item_id=row["item_id"],
            confidence=row["confidence"].lower(),
            evidence=row["evidence"].strip()[:EVIDENCE_MAX_CHARS],
        )
        for row in sorted(rows, key=lambda row: row["index"])
    )
    return BatchResult(decisions, None, input_tokens, output_tokens)
