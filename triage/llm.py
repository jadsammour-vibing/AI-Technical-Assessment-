"""Minimal OpenAI-compatible chat client (stdlib only).

Works with Groq, OpenAI, Ollama, Gemini's OpenAI-compatible endpoint, or any
other /chat/completions server. Choose a provider with the LLM_PROVIDER env var,
or override LLM_BASE_URL / LLM_MODEL / LLM_API_KEY directly.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "GEMINI_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "llama3.1:8b", None),
}


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, provider: str | None = None):
        provider = (provider or os.getenv("LLM_PROVIDER", "groq")).lower()
        if provider not in PROVIDERS and not os.getenv("LLM_BASE_URL"):
            raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Options: {', '.join(PROVIDERS)}")
        base, model, key_env = PROVIDERS.get(provider, ("", "", None))
        self.provider = provider
        self.base_url = os.getenv("LLM_BASE_URL", base).rstrip("/")
        self.model = os.getenv("LLM_MODEL", model)
        self.api_key = os.getenv("LLM_API_KEY") or (os.getenv(key_env) if key_env else None) or "none"
        self.timeout = float(os.getenv("LLM_TIMEOUT", "30"))

    def chat_json(self, messages: list[dict], max_retries: int = 6) -> dict:
        """Call the model in JSON mode, temperature 0. Retries on 429/5xx/timeouts."""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(body).encode()
        last_err: Exception | None = None
        for attempt in range(max_retries):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}",
                         "User-Agent": "arcvault-triage/1.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read())
                content = payload["choices"][0]["message"]["content"]
                return parse_json_object(content)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                last_err = LLMError(f"HTTP {e.code}: {detail}")
                if e.code not in (408, 429, 500, 502, 503, 504):
                    raise last_err
                wait = _retry_after(e, detail)
                if wait is not None:  # the server told us how long to wait: respect it
                    time.sleep(min(wait + random.uniform(0.2, 1.0), 60))
                    continue
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
                last_err = e
            time.sleep(2 ** attempt + random.uniform(0, 0.5))  # exponential backoff + jitter
        raise LLMError(f"LLM call failed after {max_retries} attempts: {last_err}")


def parse_json_object(text: str) -> dict:
    """Parse a JSON object, tolerating markdown fences some models add despite JSON mode."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    return json.loads(text[start : end + 1])


def _retry_after(err: urllib.error.HTTPError, detail: str) -> float | None:
    """Seconds to wait from the Retry-After header or a 'try again in 4.38s' message (Groq)."""
    header = err.headers.get("Retry-After") if err.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", detail)
    if m:
        secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
        return secs + 60 * int(m.group(1) or 0)
    return None
