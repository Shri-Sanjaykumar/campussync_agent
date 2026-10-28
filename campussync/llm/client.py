"""DeepSeek client (OpenAI-compatible API). Every call is wrapped so that any failure
(no key, no network, bad JSON) falls back to the rule-based path instead of crashing."""
from __future__ import annotations

import json
import os
import time as _time
from typing import Any


class LLMClient:
    def __init__(self, settings):
        self.settings = settings
        self.base_url = os.getenv("DEEPSEEK_BASE_URL") or settings.get("llm.base_url", "https://api.deepseek.com")
        self.model = os.getenv("DEEPSEEK_MODEL") or settings.get("llm.model", "deepseek-chat")
        self.temperature = float(settings.get("llm.temperature", 0.2))
        self.timeout = float(settings.get("llm.timeout_s", 30))
        self.mode = str(settings.get("llm.use_llm", "auto")).lower()
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.last_error: str | None = None
        self.calls = 0
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.api_key)

    def set_key(self, key: str | None) -> None:
        self.api_key = (key or "").strip()
        self._client = None
        self.last_error = None

    def status(self) -> str:
        if self.mode == "off":
            return "off (rule-based mode)"
        if not self.api_key:
            return "no API key (rule-based mode)"
        if self.last_error:
            return f"error, falling back to rules: {self.last_error[:120]}"
        return f"DeepSeek ready ({self.model})"

    def _get(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout, max_retries=1)
        return self._client

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             temperature: float | None = None) -> Any:
        """Returns the assistant message object, or raises. Callers catch and fall back."""
        if not self.enabled:
            raise RuntimeError("LLM not enabled")
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages,
                                  "temperature": self.temperature if temperature is None else temperature}
        if tools:
            kwargs["tools"] = tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        t0 = _time.time()
        try:
            resp = self._get().chat.completions.create(**kwargs)
            self.calls += 1
            self.last_error = None
            return resp.choices[0].message
        except Exception as e:  # network, auth, rate limit...
            self.last_error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self.last_latency = _time.time() - t0

    def json(self, system: str, user: str) -> dict:
        msg = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
        text = (msg.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1]
        return json.loads(text)

    def text(self, system: str, user: str, temperature: float | None = None) -> str:
        msg = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                        temperature=temperature)
        return (msg.content or "").strip()
