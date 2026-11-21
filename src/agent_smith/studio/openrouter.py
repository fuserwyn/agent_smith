"""The OpenRouter tab of the studio: the whole catalog (one key, hundreds of models), the key's balance, and the
user's picks — models added to the chat and the default model of each media kind.

Picks live next to the studio's state of every project (``~/.cache/agent-smith-studio/openrouter.json``), so a
model chosen once is there in every project; config/models.json stays the shared baseline.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent_smith.config import keys_from_env

API = "https://openrouter.ai/api/v1"
MEDIA_KINDS = ("image", "video", "speech", "transcribe", "perceive", "embeddings")
_CACHE: Dict[str, Any] = {"models": None, "at": 0.0}
_LOCK = threading.Lock()


def picks_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "agent-smith-studio" / "openrouter.json"


def load_picks() -> Dict[str, Any]:
    try:
        data = json.loads(picks_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    chat = [m for m in data.get("chat", []) if isinstance(m, str)]
    media = {k: v for k, v in (data.get("media") or {}).items() if k in MEDIA_KINDS and isinstance(v, str)}
    return {"chat": chat, "media": media}


def save_picks(chat: List[str], media: Dict[str, str]) -> Dict[str, Any]:
    clean = {"chat": list(dict.fromkeys(m.strip() for m in chat if m and m.strip()))[:40],
             "media": {k: v for k, v in media.items() if k in MEDIA_KINDS and v}}
    path = picks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return clean


def _price(p: Dict[str, Any], key: str) -> Optional[float]:
    try:
        v = float(p.get(key) or 0)
    except (TypeError, ValueError):
        return None
    return round(v * 1e6, 4) if v >= 0 else None


def _kinds(inp: List[str], out: List[str]) -> List[str]:
    k = [m for m in ("image", "video", "audio") if m in out]
    if "text" in out and not k:
        k.append("text")
    return k


def catalog() -> List[Dict[str, Any]]:
    """Every model of OpenRouter (chat + embeddings), cached for an hour. Public endpoints: no key needed."""
    with _LOCK:
        if _CACHE["models"] is not None and time.time() - _CACHE["at"] < 3600:
            return _CACHE["models"]
    if os.environ.get("AGENT_SMITH_OFFLINE"):
        return []
    out: List[Dict[str, Any]] = []
    try:
        chat = httpx.get(API + "/models", timeout=20).json()["data"]
    except (httpx.HTTPError, ValueError, KeyError):
        chat = []
    try:
        emb = httpx.get(API + "/embeddings/models", timeout=20).json()["data"]
    except (httpx.HTTPError, ValueError, KeyError):
        emb = []
    for m, is_emb in [(m, False) for m in chat] + [(m, True) for m in emb]:
        p, a = m.get("pricing") or {}, m.get("architecture") or {}
        inp, outm = a.get("input_modalities") or ["text"], a.get("output_modalities") or ["text"]
        if m["id"].endswith((":batch", ":free")):  # batch API: not usable from a chat; free tier: not used here
            continue
        pin, pout = _price(p, "prompt"), _price(p, "completion")
        if pin is None or (pin < 0):  # routers with variable prices
            continue
        if not pin and not pout and not _price(p, "image_output") and not _price(p, "request"):
            continue  # $0: a free router ("openrouter/free") or a preview — this tab is the paid key, priced models only
        out.append({
            "id": m["id"], "name": m.get("name") or m["id"], "created": m.get("created") or 0,
            "context": m.get("context_length") or 0, "in": inp, "out": ["embeddings"] if is_emb else outm,
            "kinds": ["embeddings"] if is_emb else _kinds(inp, outm),
            "price": {"in": pin, "out": pout, "image_out": _price(p, "image_output"), "audio": _price(p, "audio")},
            "free": False,
            "tools": "tools" in (m.get("supported_parameters") or []),
            "description": " ".join(str(m.get("description") or "").split())[:400],
        })
    out.sort(key=lambda x: -x["created"])
    if out:
        with _LOCK:
            _CACHE.update(models=out, at=time.time())
    return out


_ROUTES: Dict[str, Optional[Dict[str, float]]] = {}  # "vendor/model@host" -> that host's price ($ per 1M) or None


def route_prices(ids: List[str], on_ready: Optional[Any] = None) -> Dict[str, Dict[str, float]]:
    """Prices of OpenRouter models pinned to one host (``vendor/model@host``), from the model's endpoints.
    Returns what is known now; the missing ones are fetched in the background, then ``on_ready()`` is called."""
    missing = [i for i in ids if i not in _ROUTES]
    if missing and not os.environ.get("AGENT_SMITH_OFFLINE"):
        with _LOCK:
            for i in missing:
                _ROUTES[i] = None

        def fetch() -> None:
            for mid in missing:
                model, _, host = mid.rpartition("@")
                try:
                    eps = httpx.get(f"{API}/models/{model}/endpoints", timeout=20).json()["data"]["endpoints"]
                    ep = next(e for e in eps if (e.get("tag") or "").split("/")[0] == host
                              or str(e.get("provider_name", "")).lower() == host)
                    _ROUTES[mid] = {"in": _price(ep["pricing"], "prompt") or 0.0, "out": _price(ep["pricing"], "completion") or 0.0}
                except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration):
                    with _LOCK:
                        _ROUTES.pop(mid, None)  # retried on the next listing
            if on_ready:
                on_ready()
        threading.Thread(target=fetch, daemon=True).start()
    return {i: p for i, p in _ROUTES.items() if i in ids and p}


def account() -> Dict[str, Any]:
    """Balance of the key (numbers only; the key itself never leaves the server)."""
    keys = keys_from_env("OPENROUTER_API_KEY")
    if not keys:
        return {"key": False}
    h = {"Authorization": f"Bearer {keys[0]}"}
    info: Dict[str, Any] = {"key": True}
    try:
        d = httpx.get(API + "/key", headers=h, timeout=15).json().get("data") or {}
        info.update(usage=d.get("usage"), limit=d.get("limit"), remaining=d.get("limit_remaining"),
                    free_tier=d.get("is_free_tier"), label=None)
    except (httpx.HTTPError, ValueError):
        info["error"] = "OpenRouter does not answer"
    try:
        c = httpx.get(API + "/credits", headers=h, timeout=15).json().get("data") or {}
        if "total_credits" in c:
            info.update(credits=c.get("total_credits"), used=c.get("total_usage"),
                        balance=round(float(c.get("total_credits") or 0) - float(c.get("total_usage") or 0), 4))
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return info
