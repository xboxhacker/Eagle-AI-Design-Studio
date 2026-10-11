"""Claude API cost tracking: per-call, per-session and per-project running totals.

Prices are USD per million tokens, copied from the Anthropic pricing page on
2026-10-07. They live in pricing.json next to the project so you can edit them
when prices change; nothing here is hard-wired except the defaults.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

DEFAULT_PRICING: Dict[str, Dict] = {
    # model id: input, output, 5-minute cache write, cache read ($/MTok)
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 0.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.10},
    # Haiku 5.5 is cheaper for prompts <= 100k tokens, dearer above
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01,
                         "long_threshold": 100000,
                         "long": {"input": 0.50, "output": 2.50, "cache_write": 0.625, "cache_read": 0.05}},
    # OpenAI (openai.com/api/pricing, 2026-10-08; standard tier, <270k context). Patterns match the API model ids.
    "*gpt-5.6*sol*": {"input": 5.0, "output": 30.0, "cache_write": 5.0, "cache_read": 0.50},
    "*gpt-5.6*terra*": {"input": 2.0, "output": 12.0, "cache_write": 2.0, "cache_read": 0.20},
    "*gpt-5.6*luna*": {"input": 0.20, "output": 1.20, "cache_write": 0.20, "cache_read": 0.02},
    # xAI (docs.x.ai, 2026-10-08). Cached price not published - charged as full input to be safe.
    "grok-4.7*": {"input": 2.0, "output": 6.0, "cache_write": 2.0, "cache_read": 2.0},
}


@dataclass
class CallCost:
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    usd: float
    ts: float = field(default_factory=time.time)
    label: str = ""


class CostTracker:
    def __init__(self, project_dir: Optional[Path] = None, budget_usd: Optional[float] = None):
        self.project_dir = Path(project_dir) if project_dir else None
        self.pricing: Dict[str, Dict] = json.loads(json.dumps(DEFAULT_PRICING))
        self.budget_usd = budget_usd
        self.calls: list[CallCost] = []
        self.history_usd = 0.0
        self.listeners = []
        self.unpriced: set = set()
        if self.project_dir:
            self._load()

    # --------------------------------------------------------------- storage
    def _paths(self):
        return self.project_dir / "pricing.json", self.project_dir / "usage_log.jsonl"

    def _load(self):
        pricing, log = self._paths()
        if pricing.exists():
            try:
                saved = json.loads(pricing.read_text(encoding="utf-8"))
                self.pricing.update(saved)
                if any(k not in saved for k in DEFAULT_PRICING):     # add newly known models to the file
                    pricing.write_text(json.dumps(dict(DEFAULT_PRICING, **saved), indent=2), encoding="utf-8")
            except Exception:
                pass
        else:
            try:
                pricing.write_text(json.dumps(DEFAULT_PRICING, indent=2), encoding="utf-8")
            except OSError:
                pass
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    self.history_usd += json.loads(line)["usd"]
                except Exception:
                    pass

    # ------------------------------------------------------------------ math
    def price_for(self, model: str, prompt_tokens: int) -> Optional[Dict]:
        p = self.pricing.get(model)
        if p is None:   # wildcard keys, most specific (longest) first
            import fnmatch
            for k in sorted((k for k in self.pricing if "*" in k), key=len, reverse=True):
                if fnmatch.fnmatch(model, k):
                    p = self.pricing[k]
                    break
        if p is None:
            return None
        if "long" in p and prompt_tokens > p.get("long_threshold", 1 << 60):
            return p["long"]
        return p

    def cost(self, model: str, input_tokens: int, output_tokens: int,
             cache_write: int = 0, cache_read: int = 0) -> float:
        prompt = input_tokens + cache_write + cache_read
        p = self.price_for(model, prompt)
        if p is None:
            self.unpriced.add(model)
            return 0.0
        return (input_tokens * p["input"] + output_tokens * p["output"]
                + cache_write * p["cache_write"] + cache_read * p["cache_read"]) / 1e6

    def record(self, model: str, usage, label: str = "", extra_usd: float = 0.0) -> CallCost:
        """`usage` is the `usage` object (or dict) from a Messages API response."""
        def g(name: str) -> int:
            v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
            return int(v or 0)
        c = CallCost(model, g("input_tokens"), g("output_tokens"),
                     g("cache_creation_input_tokens"), g("cache_read_input_tokens"), 0.0, label=label)
        c.usd = self.cost(model, c.input_tokens, c.output_tokens, c.cache_write_tokens, c.cache_read_tokens) + extra_usd
        self.calls.append(c)
        if self.project_dir:
            try:
                with open(self._paths()[1], "a", encoding="utf-8") as f:
                    f.write(json.dumps(c.__dict__) + "\n")
            except OSError:
                pass
        for cb in self.listeners:
            cb(self)
        return c

    # ---------------------------------------------------------------- totals
    @property
    def session_usd(self) -> float:
        return sum(c.usd for c in self.calls)

    @property
    def project_usd(self) -> float:
        return self.history_usd + self.session_usd

    @property
    def saved_usd(self) -> float:
        """What prompt caching saved this session vs paying full input price."""
        tot = 0.0
        for c in self.calls:
            pr = self.price_for(c.model, c.input_tokens + c.cache_read_tokens + c.cache_write_tokens)
            if pr:
                tot += c.cache_read_tokens * (pr["input"] - pr["cache_read"]) / 1e6
        return tot

    def over_budget(self) -> bool:
        return self.budget_usd is not None and self.session_usd >= self.budget_usd

    def estimate(self, model: str, input_tokens: int, expected_output: int = 2000) -> float:
        return self.cost(model, input_tokens, expected_output)

    def summary(self) -> str:
        tin = sum(c.input_tokens + c.cache_read_tokens + c.cache_write_tokens for c in self.calls)
        tout = sum(c.output_tokens for c in self.calls)
        return (f"session ${self.session_usd:.4f} · project ${self.project_usd:.4f} · "
                f"{len(self.calls)} calls · {tin:,} in / {tout:,} out · cache saved ${self.saved_usd:.4f}"
                + (f" · NO PRICE for {', '.join(sorted(self.unpriced))} (edit pricing.json)" if self.unpriced else ""))
