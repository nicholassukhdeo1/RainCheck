"""Thin helpers around the Anthropic SDK: client construction, safe text extraction, cost.

Nothing here imports `anthropic` at module level.  Only `make_client` does, so every
module that merely imports this one (including a future Lambda import graph) works
without the SDK installed and can never reach the network by accident.
"""
import os

# The user's choice of model.
DEFAULT_MODEL = "claude-opus-5-5"

# Thinking cannot be disabled on Opus 5.5, so effort is the control for thinking depth and cost.
DEFAULT_EFFORT = "low"

# The SDK default is 10 minutes.  A non-streaming request returns no bytes until generation
# finishes, so this must cover a full 25-title response but still bound a hung connection.
REQUEST_TIMEOUT_SECONDS = 90.0

# The SDK default is 2.  One retry absorbs a transient error (connection, 408, 409, 429, 5xx)
# while keeping the worst case near 2 * REQUEST_TIMEOUT_SECONDS per request.
MAX_RETRIES = 1

# (input, output) USD per million tokens, from the pricing page as of 2026-11-18.
# The keys are the only models the evaluation accepts: cost cannot be estimated for others.
PRICES_USD_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def api_key_from_env():
    """The API key from the environment, or None when unset or blank.  Never logged."""
    value = os.environ.get("ANTHROPIC_API_KEY")
    if value is None or not value.strip():
        return None
    return value


def make_client(api_key):
    """Build an Anthropic client with an explicit timeout and retry count."""
    # Check before importing so a bad key never costs an import (or hides behind ImportError).
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-blank string")
    import anthropic  # lazy: see the module docstring

    # The key is passed explicitly so a stray ANTHROPIC_* variable cannot swap credentials.
    return anthropic.Anthropic(
        api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES
    )


def response_text(response):
    """Return (text, None) for a complete answer, else (None, reason).

    The stop reason is checked first: on "refusal" and "max_tokens" the output might not
    match the schema, so it must not be parsed.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        return None, "refusal"
    if stop_reason == "max_tokens":
        return None, "max_tokens"
    if stop_reason != "end_turn":
        return None, "unexpected_stop"

    # Thinking blocks (and any other block type) are ignored; only text blocks are the answer.
    parts = [
        block.text
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text"
    ]
    if not parts:
        return None, "no_text"
    return "".join(parts), None


def _token_count(value):
    # bool is an int subclass, so it is excluded explicitly.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def usage_tokens(response):
    """(input_tokens, output_tokens) from the response, or (0, 0) if either is unusable."""
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if _token_count(input_tokens) and _token_count(output_tokens):
        return input_tokens, output_tokens
    return 0, 0


def cost_usd(model, input_tokens, output_tokens):
    """Dollar cost of a request.  Raises KeyError for a model with no known price."""
    input_price, output_price = PRICES_USD_PER_MTOK[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
