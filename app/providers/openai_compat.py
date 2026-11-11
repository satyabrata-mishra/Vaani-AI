"""One client for any OpenAI-compatible chat endpoint: Groq (source='real') and our stand-in
(source='stand-in'). Uses raw httpx so that every attempt, header and timing is visible."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import RESULTS_DIR, Settings
from app.providers.base import (
    CLIENT, CONNECTION, RATE_LIMITED, SERVER, TIMEOUT_AFTER_FIRST_TOKEN,
    TIMEOUT_BEFORE_FIRST_TOKEN, CallResult, ProviderError, RateLimitInfo, limit_kind_from_message,
)

SPEND_LEDGER = RESULTS_DIR / "groq_spend.jsonl"


class StreamCall:
    """`async for delta in call` yields answer text; `call.result` holds the measurements,
    also after a ProviderError (which carries the same result object)."""

    def __init__(self, provider: "OpenAICompatProvider", payload: dict,
                 timeout_s: float, first_token_timeout_s: float | None):
        self._p = provider
        self._payload = payload
        self._timeout_s = timeout_s
        self._ft_timeout_s = first_token_timeout_s
        self.result = CallResult(source=provider.source, model=payload["model"], t_start=0.0)

    def __aiter__(self):
        return self._run()

    def _wait_budget(self, deadline: float, ft_deadline: float | None) -> float:
        now = time.monotonic()
        limit = deadline
        if ft_deadline is not None and self.result.t_first_token is None:
            limit = min(limit, ft_deadline)
        return limit - now

    async def _run(self):
        r = self.result
        r.t_start = time.monotonic()
        deadline = r.t_start + self._timeout_s
        ft_deadline = r.t_start + self._ft_timeout_s if self._ft_timeout_s else None
        resp = None
        try:
            req = self._p.client.build_request("POST", "/chat/completions", json=self._payload)
            budget = self._wait_budget(deadline, ft_deadline)
            if budget <= 0:
                raise asyncio.TimeoutError
            resp = await asyncio.wait_for(self._p.client.send(req, stream=True), budget)
            r.t_headers = time.monotonic()
            self._p._record_headers(r, resp)
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                self._p._classify_http_error(r, resp.status_code, body)
                raise ProviderError(r)

            lines = resp.aiter_lines()
            while True:
                budget = self._wait_budget(deadline, ft_deadline)
                if budget <= 0:
                    raise asyncio.TimeoutError
                try:
                    line = await asyncio.wait_for(lines.__anext__(), budget)
                except StopAsyncIteration:
                    break
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    r.error_kind = SERVER
                    r.error_message = json.dumps(chunk["error"])[:500]
                    raise ProviderError(r)
                usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
                if usage:
                    self._p._record_usage(r, usage)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning"):
                        if r.t_first_reasoning is None:
                            r.t_first_reasoning = time.monotonic()
                        r.reasoning_chars += len(delta["reasoning"])
                    text = delta.get("content")
                    if text:
                        if r.t_first_token is None:
                            r.t_first_token = time.monotonic()
                        r.content += text
                        yield text
                    if choice.get("finish_reason"):
                        r.finish_reason = choice["finish_reason"]
            r.ok = True
        except ProviderError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            r.error_kind = TIMEOUT_BEFORE_FIRST_TOKEN if r.t_first_token is None else TIMEOUT_AFTER_FIRST_TOKEN
            r.error_message = type(e).__name__
            raise ProviderError(r) from None
        except (httpx.TransportError, json.JSONDecodeError) as e:
            r.error_kind = CONNECTION
            r.error_message = f"{type(e).__name__}: {e}"[:500]
            raise ProviderError(r) from None
        finally:
            r.t_end = time.monotonic()
            if resp is not None:
                await resp.aclose()
            self._p._ledger(r)


class OpenAICompatProvider:
    def __init__(self, base_url: str, api_key: str, model: str, source: str,
                 settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 ledger_path: Path | None = None):
        if source not in ("real", "stand-in"):
            raise ValueError(source)
        if source == "real" and not api_key:
            raise RuntimeError("GROQ_API_KEY is not set (put it in .env)")
        self.source = source
        self.model = model
        self.settings = settings
        self.ledger_path = ledger_path if ledger_path is not None else (SPEND_LEDGER if source == "real" else None)
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key or 'stand-in'}"},
            timeout=httpx.Timeout(connect=settings.provider_connect_timeout_s, read=None, write=10.0, pool=None),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            transport=transport,
        )

    @classmethod
    def groq(cls, settings: Settings, **kw) -> "OpenAICompatProvider":
        return cls(settings.groq_base_url, settings.groq_api_key, settings.model, "real", settings, **kw)

    @classmethod
    def standin(cls, settings: Settings, **kw) -> "OpenAICompatProvider":
        return cls(settings.standin_base_url, "", settings.model, "stand-in", settings, **kw)

    async def aclose(self):
        await self.client.aclose()

    def build_payload(self, messages: list[dict], max_tokens: int, stream: bool,
                      temperature: float = 0.2) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if self.model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = self.settings.reasoning_effort
            payload["include_reasoning"] = True
        return payload

    def stream_chat(self, messages: list[dict], *, max_tokens: int, timeout_s: float,
                    first_token_timeout_s: float | None = None, temperature: float = 0.2) -> StreamCall:
        return StreamCall(self, self.build_payload(messages, max_tokens, True, temperature),
                          timeout_s, first_token_timeout_s)

    async def complete(self, messages: list[dict], *, max_tokens: int, timeout_s: float,
                       temperature: float = 0.2) -> CallResult:
        """Non-streaming call. For consistency with streaming, first-token time = time the full
        body arrived (the user cannot see anything earlier)."""
        r = CallResult(source=self.source, model=self.model, t_start=time.monotonic())
        try:
            resp = await asyncio.wait_for(
                self.client.post("/chat/completions",
                                 json=self.build_payload(messages, max_tokens, False, temperature)),
                timeout_s)
            r.t_headers = time.monotonic()
            self._record_headers(r, resp)
            if resp.status_code != 200:
                self._classify_http_error(r, resp.status_code, resp.text)
                raise ProviderError(r)
            body = resp.json()
            choice = (body.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            r.content = msg.get("content") or ""
            r.reasoning_chars = len(msg.get("reasoning") or "")
            r.finish_reason = choice.get("finish_reason")
            if body.get("usage"):
                self._record_usage(r, body["usage"])
            r.t_first_token = time.monotonic()
            r.ok = True
            return r
        except ProviderError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            r.error_kind, r.error_message = TIMEOUT_BEFORE_FIRST_TOKEN, type(e).__name__
            raise ProviderError(r) from None
        except (httpx.TransportError, ValueError) as e:
            r.error_kind, r.error_message = CONNECTION, f"{type(e).__name__}: {e}"[:500]
            raise ProviderError(r) from None
        finally:
            r.t_end = time.monotonic()
            self._ledger(r)

    # --- helpers -------------------------------------------------------------------------
    @staticmethod
    def _record_headers(r: CallResult, resp: httpx.Response):
        r.status_code = resp.status_code
        r.raw_headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("set-cookie",)}
        r.rate_limit = RateLimitInfo.from_headers(resp.headers)

    @staticmethod
    def _record_usage(r: CallResult, usage: dict):
        r.tokens_in = usage.get("prompt_tokens")
        r.tokens_out = usage.get("completion_tokens")
        details = usage.get("completion_tokens_details") or {}
        r.reasoning_tokens = details.get("reasoning_tokens")

    @staticmethod
    def _classify_http_error(r: CallResult, status: int, body: str):
        message = body
        try:
            message = json.loads(body).get("error", {}).get("message", body)
        except (ValueError, AttributeError):
            pass
        r.error_message = str(message)[:1000]
        if status == 429:
            r.error_kind = RATE_LIMITED
            r.limit_kind = limit_kind_from_message(r.error_message)
        elif status >= 500 or status == 498:  # 498 = Groq flex-tier capacity exceeded
            r.error_kind = SERVER
        else:
            r.error_kind = CLIENT

    def _ledger(self, r: CallResult):
        if self.ledger_path is None:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "utc": datetime.now(timezone.utc).isoformat(), "model": r.model,
                "status_code": r.status_code, "error_kind": r.error_kind,
                "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            }) + "\n")
