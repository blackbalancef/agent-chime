from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from agent_voice.config import AgentVoiceConfig, DEFAULT_SUMMARY_PROMPT, language_display_name
from agent_voice.models import SessionStatus
from agent_voice.secrets import resolve_openai_api_key


@dataclass(frozen=True, slots=True)
class SummaryResult:
    message: str | None = None
    cost_usd: float = 0.0
    input_text_tokens: int = 0
    cached_input_text_tokens: int = 0
    output_text_tokens: int = 0
    request_id: str | None = None
    client_request_id: str | None = None
    error: str | None = None
    prompt: str | None = None
    raw_text: str | None = None


def summarize_notification(config: AgentVoiceConfig, candidate: object) -> SummaryResult:
    if not _should_summarize(config, candidate):
        return SummaryResult()

    source_text = _source_text(config, candidate)
    if not source_text:
        return SummaryResult()

    api_key, secret_status = resolve_openai_api_key(config)
    if not api_key:
        return SummaryResult(error="OpenAI API key is missing")

    prompt = _format_prompt(config, candidate, source_text)
    client_request_id = uuid.uuid4().hex
    payload = {
        "model": config.summary_model,
        "input": prompt,
        "store": False,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Agent-Voice-Key-Source": secret_status.source,
            "X-Client-Request-Id": client_request_id,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.summary_timeout_seconds) as response:
            request_id = response.headers.get("x-request-id")
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return SummaryResult(
            client_request_id=client_request_id,
            error=f"HTTP {exc.code}: {body[:300]}",
            prompt=prompt,
        )
    except Exception as exc:  # pragma: no cover - network/platform dependent
        return SummaryResult(client_request_id=client_request_id, error=str(exc), prompt=prompt)

    raw_text = _extract_response_text(data)
    message = _clean_model_text(raw_text)
    usage = data.get("usage") if isinstance(data, dict) else None
    input_tokens = _usage_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_int(usage, "output_tokens", "completion_tokens")
    cached_tokens = _cached_input_tokens(usage)
    cost_usd = _estimate_cost(config, input_tokens, cached_tokens, output_tokens)
    return SummaryResult(
        message=message,
        cost_usd=cost_usd,
        input_text_tokens=input_tokens,
        cached_input_text_tokens=cached_tokens,
        output_text_tokens=output_tokens,
        request_id=request_id,
        client_request_id=client_request_id,
        prompt=prompt,
        raw_text=raw_text,
    )


def _should_summarize(config: AgentVoiceConfig, candidate: object) -> bool:
    if not config.summary_enabled or config.summary_provider != "openai":
        return False
    return getattr(candidate, "status", None) == SessionStatus.COMPLETED


def _source_text(config: AgentVoiceConfig, candidate: object) -> str | None:
    if config.summary_privacy_level == "full_last_message":
        source = getattr(candidate, "summary_source_text", None) or getattr(candidate, "message", None)
    else:
        source = getattr(candidate, "message", None)
    if not source:
        return None
    text = str(source).strip()
    return limit_summary_source_text(text, max_chars=config.summary_max_input_chars) or None


def limit_summary_source_text(source_text: str, *, max_chars: int) -> str:
    text = str(source_text or "").strip()
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text

    marker = "\n\n[... omitted middle ...]\n\n"
    if max_chars <= len(marker) + 20:
        return text[: max(0, max_chars - 3)].rstrip(" ,;:.") + "..."

    content_budget = max_chars - len(marker)
    head_chars = content_budget // 2
    tail_chars = content_budget - head_chars
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    return f"{head}{marker}{tail}"


def _format_prompt(config: AgentVoiceConfig, candidate: object, source_text: str) -> str:
    values = {
        "agent": getattr(candidate, "agent_name", "") or "Agent",
        "project": getattr(candidate, "project_name", "") or "session",
        "status": getattr(getattr(candidate, "status", None), "value", getattr(candidate, "status", "")),
        "language": config.language,
        "language_name": language_display_name(config.language),
        "max_words": config.summary_max_words,
        "message": source_text,
    }
    template = config.summary_prompt or DEFAULT_SUMMARY_PROMPT
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return DEFAULT_SUMMARY_PROMPT.format(**values)


def _extract_response_text(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    fragments: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text)
    if fragments:
        return " ".join(fragments)

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]
    return None


def _clean_model_text(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.strip().split()).strip(" \"'")
    return text.rstrip(".") + "." if text and not text.endswith((".", "!", "?")) else text or None


def _usage_int(usage: Any, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int | float):
            return max(0, int(value))
    return 0


def _cached_input_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or details.get("cached_input_tokens")
        if isinstance(cached, int | float):
            return max(0, int(cached))
    cached = usage.get("cached_input_tokens")
    if isinstance(cached, int | float):
        return max(0, int(cached))
    return 0


def _estimate_cost(
    config: AgentVoiceConfig,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> float:
    cached = min(cached_input_tokens, input_tokens)
    uncached_input = max(0, input_tokens - cached)
    return (
        uncached_input * config.summary_text_input_price_per_million_tokens_usd
        + cached * config.summary_cached_input_price_per_million_tokens_usd
        + output_tokens * config.summary_text_output_price_per_million_tokens_usd
    ) / 1_000_000
