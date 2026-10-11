"""Which models each AI brand offers right now, and which are new to this PC."""
from __future__ import annotations

from typing import Dict, List

from .providers import BRANDS, DEFAULT_MODEL, make_provider

BUILTIN = {"claude": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-fable-5-1"],
           "openai": [], "xai": ["grok-4.7"]}


def fetch_all() -> Dict[str, List[Dict]]:
    """{brand: [{id, name, created}]} for every brand that has a key. Errors per brand are returned as text."""
    out: Dict[str, List[Dict]] = {}
    for brand in BRANDS:
        prov = make_provider(brand, DEFAULT_MODEL[brand] or "x")
        if prov is None:
            continue
        try:
            out[brand] = prov.list_models()
        except Exception as exc:
            out[brand] = [{"error": f"{type(exc).__name__}: {exc}"}]
    return out


def diff_new(found: List[Dict], known: List[str], brand: str = "claude") -> List[Dict]:
    k = set(known) | set(BUILTIN.get(brand, []))
    return [m for m in found if "id" in m and m["id"] not in k]
