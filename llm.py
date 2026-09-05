"""
LLM Provider Shim
=================
One place that talks to a model. Everything else calls `chat()`.

Any OpenAI-compatible endpoint works — Groq, OpenAI, Together, Fireworks,
OpenRouter, or a local vLLM/Ollama server — because the `openai` SDK is used as
the client and the provider is chosen by `base_url`. Switching is a `.env` edit,
not a code change:

    LLM_BASE_URL=https://api.groq.com/openai/v1     # Groq   (default)
    LLM_BASE_URL=                                    # OpenAI (SDK default)
    LLM_BASE_URL=http://localhost:11434/v1           # Ollama
    LLM_API_KEY=...
    LLM_MODEL=openai/gpt-oss-20b

`GROQ_API_KEY` / `GROQ_MODEL` continue to work, so existing setups are
unaffected.

The one genuinely provider-specific parameter is `reasoning_effort`: Groq
accepts it for gpt-oss and it is worth ~4x on latency, while OpenAI rejects it
on non-reasoning models with a 400. Rather than hardcoding which models take it,
the first rejection is caught, remembered per model, and the call retried
without it — so an unknown provider costs one wasted request, once, instead of
failing outright.
"""

import os
import threading

import config

# Quirks learned at runtime, so a new provider or model self-configures instead
# of needing a lookup table that goes stale:
#
#   reasoning_effort  -- Groq accepts it for gpt-oss (worth ~4x on latency);
#                        OpenAI rejects it on non-reasoning models.
#   max_tokens        -- OpenAI's reasoning models (gpt-5-*, o-series) require
#                        `max_completion_tokens` and reject `max_tokens`
#                        outright, and also reject temperature != 1.
#
# Each rejection costs one wasted request, once per model.
_NO_REASONING_EFFORT = set()
_NEEDS_MAX_COMPLETION_TOKENS = set()
_NO_TEMPERATURE = set()
_LOCK = threading.Lock()
_CLIENT = None
_CLIENT_KEY = None


class LLMUnavailable(RuntimeError):
    """No key configured, or the provider refused the request."""


def api_key() -> str:
    """
    LLM_API_KEY wins; the provider-specific names are accepted so an existing
    .env keeps working. OPENAI_API_KEY is only used when the endpoint is
    actually OpenAI -- sending an OpenAI key to Groq just produces a confusing
    401.
    """
    explicit = os.getenv("LLM_API_KEY", "") or config.LLM_API_KEY
    if explicit:
        return explicit
    url = base_url()
    if not url or "openai.com" in url:
        return os.getenv("OPENAI_API_KEY", "")
    if "groq" in url:
        return os.getenv("GROQ_API_KEY", "")
    return os.getenv("GROQ_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")


def base_url() -> str:
    """Empty means the SDK default, which is OpenAI."""
    return (os.getenv("LLM_BASE_URL", None)
            if os.getenv("LLM_BASE_URL") is not None else config.LLM_BASE_URL) or ""


def model() -> str:
    return (os.getenv("LLM_MODEL", "") or os.getenv("GROQ_MODEL", "")
            or config.ACTIVE_MODEL)


def is_configured() -> bool:
    return bool(api_key())


def provider_name() -> str:
    """Best-effort label for the UI and logs."""
    url = base_url()
    if not url:
        return "OpenAI"
    for needle, label in (("groq", "Groq"), ("openai.com", "OpenAI"),
                          ("together", "Together"), ("fireworks", "Fireworks"),
                          ("openrouter", "OpenRouter"), ("localhost", "local"),
                          ("127.0.0.1", "local")):
        if needle in url:
            return label
    return url.split("//")[-1].split("/")[0]


def describe() -> str:
    return f"{model()} on {provider_name()}"


def _client():
    """Cached client; rebuilt when the key or endpoint changes."""
    global _CLIENT, _CLIENT_KEY
    key, url = api_key(), base_url()
    if not key:
        raise LLMUnavailable("No API key. Set LLM_API_KEY (or GROQ_API_KEY).")
    with _LOCK:
        signature = (key, url)
        if _CLIENT is None or _CLIENT_KEY != signature:
            from openai import OpenAI
            _CLIENT = OpenAI(api_key=key, base_url=url or None,
                             timeout=config.LLM_TIMEOUT_S,
                             max_retries=config.LLM_MAX_RETRIES)
            _CLIENT_KEY = signature
    return _CLIENT


def reset():
    """Drops the cached client. Used by tests that swap providers mid-process."""
    global _CLIENT, _CLIENT_KEY
    with _LOCK:
        _CLIENT, _CLIENT_KEY = None, None


def _mentions(err: Exception, *needles) -> bool:
    msg = str(err).lower()
    return any(n in msg for n in needles)


def _rejects_reasoning_effort(err: Exception) -> bool:
    return _mentions(err, "reasoning_effort") or (
        _mentions(err, "unsupported", "unrecognized") and _mentions(err, "reasoning"))


def _rejects_max_tokens(err: Exception) -> bool:
    return "max_tokens" in str(err).lower() and _mentions(
        err, "unsupported", "not supported", "max_completion_tokens")


def _rejects_temperature(err: Exception) -> bool:
    return "temperature" in str(err).lower() and _mentions(
        err, "unsupported", "not supported", "does not support")


def chat(messages, tools=None, tool_choice=None, temperature: float = 0.0,
         max_tokens: int = 400, reasoning_effort: str = None, model_name: str = None):
    """
    One chat completion. Returns the provider's response object.

    Raises LLMUnavailable when unconfigured; callers fall back to their
    deterministic path rather than surfacing an error to the user.
    """
    name = model_name or model()
    client = _client()

    def build():
        kw = {"model": name, "messages": messages}
        if name in _NEEDS_MAX_COMPLETION_TOKENS:
            # Reasoning models spend part of the budget on hidden reasoning
            # tokens, so a tool call needs headroom a plain completion does not.
            kw["max_completion_tokens"] = max(max_tokens, config.LLM_REASONING_MIN_TOKENS)
        else:
            kw["max_tokens"] = max_tokens
        if name not in _NO_TEMPERATURE:
            kw["temperature"] = temperature
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = tool_choice or "auto"
        if reasoning_effort and name not in _NO_REASONING_EFFORT:
            kw["reasoning_effort"] = reasoning_effort
        return kw

    # Each unsupported parameter is learned once and then never sent again for
    # that model, so at most a few requests are wasted over a process lifetime.
    for _ in range(4):
        try:
            return client.chat.completions.create(**build())
        except Exception as e:
            learned = False
            with _LOCK:
                if _rejects_reasoning_effort(e) and name not in _NO_REASONING_EFFORT:
                    _NO_REASONING_EFFORT.add(name); learned = True
                if _rejects_max_tokens(e) and name not in _NEEDS_MAX_COMPLETION_TOKENS:
                    _NEEDS_MAX_COMPLETION_TOKENS.add(name); learned = True
                if _rejects_temperature(e) and name not in _NO_TEMPERATURE:
                    _NO_TEMPERATURE.add(name); learned = True
            if not learned:
                raise
    return client.chat.completions.create(**build())


def first_tool_call(resp):
    """(name, arguments_json) of the first tool call, or None."""
    calls = resp.choices[0].message.tool_calls
    if not calls:
        return None
    call = calls[0]
    return call.function.name, (call.function.arguments or "{}")


def message_text(resp) -> str:
    return resp.choices[0].message.content or ""
