"""AI brands behind one interface: Claude (Anthropic), ChatGPT (OpenAI) and Grok (xAI).

All three get the same tools and the same project. Inside one request each provider
keeps its own native message format (full tool results); between requests the
conversation is stored provider-neutral (see agent.py), so you can switch brand
mid-project and the new one picks up from a compact summary.

OpenAI and xAI are both called through the OpenAI Python SDK's Responses API
(xAI is OpenAI-compatible at https://api.x.ai/v1).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

BRANDS = {
    # brand: (label, keyring user, env var, base_url)
    "claude": ("Claude", "anthropic", "ANTHROPIC_API_KEY", None),
    "openai": ("ChatGPT", "openai", "OPENAI_API_KEY", None),
    "xai": ("Grok", "xai", "XAI_API_KEY", "https://api.x.ai/v1"),
}
DEFAULT_MODEL = {"claude": "claude-sonnet-5-5", "openai": "", "xai": "grok-4.7"}


def get_key(brand: str) -> str:
    _, user, env, _ = BRANDS[brand]
    try:
        import keyring
        k = keyring.get_password("eagleai-ide", user)
        if k:
            return k
    except Exception:
        pass
    return os.environ.get(env, "")


def set_key(brand: str, key: str):
    import keyring
    _, user, _, _ = BRANDS[brand]
    if key:
        keyring.set_password("eagleai-ide", user, key)
    else:
        try:
            keyring.delete_password("eagleai-ide", user)
        except Exception:
            pass


@dataclass
class Step:
    text: str
    calls: List[Tuple[str, str, Dict]]          # (call_id, name, args)
    usage: Dict
    done: bool


class Provider:
    brand = ""

    def __init__(self, key: str, model: str):
        self.key, self.model = key, model

    # history: [{"role": "user"|"assistant", "text": str}] - compact, provider-neutral
    # images: attachments from images.py, sent with this message only
    def start(self, history: List[Dict], user_text: str, images: Optional[List[Dict]] = None) -> List:
        raise NotImplementedError

    def step(self, system: str, tools: List[Dict], native: List, on_text: Callable[[str], None]) -> Step:
        raise NotImplementedError

    def add_results(self, native: List, results: List[Tuple[str, str, bool]]):
        raise NotImplementedError

    def list_models(self) -> List[Dict]:
        raise NotImplementedError


def _alternate(history: List[Dict]) -> List[Dict]:
    """Merge consecutive same-role messages (some APIs require strict alternation)."""
    out: List[Dict] = []
    for h in history:
        if out and out[-1]["role"] == h["role"]:
            out[-1] = {"role": h["role"], "text": out[-1]["text"] + "\n\n" + h["text"]}
        else:
            out.append(dict(h))
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "text": "(earlier conversation)"})
    return out


# ------------------------------------------------------------------ Anthropic
class ClaudeProvider(Provider):
    brand = "claude"

    def __init__(self, key, model):
        super().__init__(key, model)
        import anthropic
        self.client = anthropic.Anthropic(api_key=key)

    def start(self, history, user_text, images=None):
        from .images import b64
        msgs = [{"role": h["role"], "content": [{"type": "text", "text": h["text"]}]} for h in _alternate(history)]
        if msgs and msgs[-1]["role"] == "user":
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "(ok)"}]})
        if msgs:   # cache breakpoint at the end of the stable history prefix
            msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        pics = [{"type": "image", "source": {"type": "base64", "media_type": a["media_type"], "data": b64(a)}}
                for a in images or []]
        msgs.append({"role": "user", "content": pics + [{"type": "text", "text": user_text}]})
        return msgs

    def _roll_cache(self, native):
        """Keep the history-prefix breakpoint and move a second one to the newest message."""
        hist_end = None
        for i, m in enumerate(native):
            for b in m["content"]:
                if isinstance(b, dict) and "cache_control" in b:
                    if hist_end is None:
                        hist_end = (i, b)
                    else:
                        b.pop("cache_control")
        last = native[-1]["content"][-1]
        if isinstance(last, dict):
            last["cache_control"] = {"type": "ephemeral"}

    web_search = True       # Claude may search jlcpcb.com / lcsc.com (server tool, $10 per 1,000 searches)

    def step(self, system, tools, native, on_text):
        from .jlc_ai import WEB_SEARCH_TOOL, WEB_SEARCH_USD
        tl = [dict(t) for t in tools]
        tl[-1] = dict(tl[-1], cache_control={"type": "ephemeral"})
        if self.web_search:
            ws = {k: v for k, v in WEB_SEARCH_TOOL.items() if k != "allowed_domains"}   # chat: any site
            tl = [dict(ws, max_uses=6)] + tl
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0, "extra_usd": 0.0}
        texts, calls, stop = [], [], "end_turn"
        for i in range(6):                        # pause_turn = server tool needs another round
            if i == 0:
                self._roll_cache(native)
            with self.client.messages.stream(model=self.model, max_tokens=4096,
                                             system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                                             tools=tl, messages=native) as stream:
                for chunk in stream.text_stream:
                    on_text(chunk)
                msg = stream.get_final_message()
            native.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in msg.content]})
            u = msg.usage
            usage["input_tokens"] += u.input_tokens or 0
            usage["output_tokens"] += u.output_tokens or 0
            usage["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
            stu = getattr(u, "server_tool_use", None)
            usage["extra_usd"] += ((getattr(stu, "web_search_requests", 0) or 0) if stu else 0) * WEB_SEARCH_USD
            texts.append("".join(b.text for b in msg.content if b.type == "text"))
            calls = [(b.id, b.name, b.input) for b in msg.content if b.type == "tool_use"]
            stop = msg.stop_reason
            if stop != "pause_turn":
                break
            # pause_turn: the assistant turn is last in `native`; sending it back as-is lets Claude continue
        return Step("".join(texts), calls, usage, stop != "tool_use")

    def add_results(self, native, results):
        native.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": out, "is_error": err} for cid, out, err in results]})

    def list_models(self):
        out = []
        for m in self.client.models.list(limit=100):
            if m.id.startswith("claude"):
                c = getattr(m, "created_at", None)
                out.append({"id": m.id, "name": getattr(m, "display_name", m.id) or m.id,
                            "created": c.isoformat() if hasattr(c, "isoformat") else str(c or "")})
        return out


# ------------------------------------------------------- OpenAI-compatible
SKIP_OPENAI = ("audio", "tts", "transcribe", "realtime", "image", "embedding", "moderation", "search",
               "dall-e", "whisper", "davinci", "babbage", "computer-use", "imagine", "vision", "codex-mini")


class ResponsesProvider(Provider):
    """OpenAI (ChatGPT) and xAI (Grok) via the Responses API."""

    def __init__(self, key, model, brand):
        super().__init__(key, model)
        self.brand = brand
        import openai
        self.client = openai.OpenAI(api_key=key, base_url=BRANDS[brand][3]) if BRANDS[brand][3] \
            else openai.OpenAI(api_key=key)

    def start(self, history, user_text, images=None):
        from .images import data_url
        items = [{"role": h["role"], "content": h["text"]} for h in _alternate(history)]
        if images:
            items.append({"role": "user", "content": [{"type": "input_text", "text": user_text}] + [
                {"type": "input_image", "image_url": data_url(a), "detail": "auto"} for a in images]})
        else:
            items.append({"role": "user", "content": user_text})
        return items

    @staticmethod
    def _tools(tools):
        return [{"type": "function", "name": t["name"], "description": t["description"],
                 "parameters": t["input_schema"]} for t in tools]

    def step(self, system, tools, native, on_text):
        resp = self.client.responses.create(model=self.model, instructions=system, input=native,
                                            tools=self._tools(tools))
        calls, texts = [], []
        for item in resp.output:
            native.append(item.model_dump(exclude_none=True))
            if item.type == "function_call":
                try:
                    args = json.loads(item.arguments or "{}")
                except ValueError:
                    args = {}
                calls.append((item.call_id, item.name, args))
            elif item.type == "message":
                for c in item.content or []:
                    if getattr(c, "type", "") == "output_text":
                        texts.append(c.text)
        text = "".join(texts)
        if text:
            on_text(text)
        u = resp.usage
        cached = 0
        if u is not None and getattr(u, "input_tokens_details", None) is not None:
            cached = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
        inp = (getattr(u, "input_tokens", 0) or 0) if u else 0
        usage = {"input_tokens": max(inp - cached, 0), "output_tokens": (getattr(u, "output_tokens", 0) or 0) if u else 0,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached}
        return Step(text, calls, usage, not calls)

    def add_results(self, native, results):
        for cid, out, _err in results:
            native.append({"type": "function_call_output", "call_id": cid, "output": out})

    def list_models(self):
        out = []
        for m in self.client.models.list():
            mid = m.id
            if self.brand == "openai" and not (mid.startswith("gpt-") or mid[:2] in ("o1", "o3", "o4", "o5")):
                continue
            if self.brand == "xai" and not mid.startswith("grok"):
                continue
            if any(s in mid for s in SKIP_OPENAI):
                continue
            out.append({"id": mid, "name": mid, "created": str(getattr(m, "created", "") or "")})
        out.sort(key=lambda d: d["created"], reverse=True)
        return out


def make_provider(brand: str, model: str, key: Optional[str] = None) -> Optional[Provider]:
    key = key if key is not None else get_key(brand)
    if not key:
        return None
    if brand == "claude":
        return ClaudeProvider(key, model or DEFAULT_MODEL["claude"])
    return ResponsesProvider(key, model or DEFAULT_MODEL[brand], brand)
