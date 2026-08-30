"""
Thin wrapper around the OpenRouter API for LLM calls.
Falls back gracefully if the API key is missing or the call fails,
so the pipeline never crashes due to LLM unavailability.
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


def call_llm(prompt: str, max_tokens: int = 300, timeout: float = 30.0) -> str | None:
    """
    Sends a prompt to the OpenRouter LLM and returns the
    text response. Strips chain-of-thought reasoning blocks
    that reasoning models emit before their final answer.
    Returns None on any failure — callers must handle None.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set — skipping LLM call")
        return None

    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    try:
        response = httpx.post(
            OPENROUTER_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            logger.warning("LLM response contained no choices")
            return None
        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            return None

        # Strip chain-of-thought reasoning blocks.
        # Reasoning models emit a scratchpad (drafting, planning,
        # "Let me think...") before the final answer. We detect
        # the boundary by finding the last blank line followed by
        # a capital letter — that is where the actual answer starts.
        # If no clear boundary exists, return the full content.
        cleaned = _strip_reasoning(content)
        return cleaned if cleaned else None

    except httpx.TimeoutException:
        logger.warning("LLM call timed out after %.1fs", timeout)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(
            "LLM call failed with status %s: %s",
            e.response.status_code,
            e.response.text[:200],
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM call failed: %s", e)
        return None


def _strip_reasoning(text: str) -> str:
    """
    Extracts the final answer from a reasoning model response
    by stripping the chain-of-thought scratchpad.

    Strategy:
    1. If the text contains a blank line, split on double
       newlines and take the last non-empty paragraph —
       reasoning models put their final answer last.
    2. If the last paragraph looks like a complete sentence
       (ends with . or "), return it.
    3. Otherwise return the full text trimmed.
    """
    text = text.strip()

    # Split into paragraphs
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return text

    # Walk backwards to find the last paragraph that looks
    # like a real answer (starts with capital, ends with punctuation,
    # is at least 40 chars — not a one-word label like "Drafting:")
    for para in reversed(paragraphs):
        is_long_enough = len(para) >= 40
        ends_with_punct = para.endswith((".", '"', "'"))
        starts_with_capital = para[0].isupper() if para else False
        not_a_label = not para.endswith(":")
        if is_long_enough and ends_with_punct and starts_with_capital and not_a_label:
            return para

    # Fallback: return the last paragraph regardless
    return paragraphs[-1]
