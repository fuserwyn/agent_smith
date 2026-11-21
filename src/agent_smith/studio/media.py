"""Attachments, voice input and non-text models of the studio (all through OpenRouter, key in Agent Smith's .env).

* Uploads are stored in the workspace under ``agent_code/uploads/`` (git-ignored, browsable, not indexed), so the
  agent can also work on them with its tools (read a CSV, process an image with code…).
* A chat model that can read an attachment's kind (image / audio / video / PDF — from the OpenRouter catalog)
  receives it natively; otherwise a cheap multimodal model ("perceive" in config/models.json) describes or
  transcribes it first and the text goes in the prompt, so attachments work with every model.
* Voice input: the browser records and converts to WAV; ``transcribe`` turns it into text.
* The catalog (capabilities and prices) is the public https://openrouter.ai/api/v1/models, cached for a day.
"""

from __future__ import annotations

import base64
import os
import mimetypes
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent_smith.codelog import FOLDER as CODE_FOLDER
from agent_smith.config import keys_from_env, load_models_config
from agent_smith.studio.workspace import Workspace, WorkspaceError

API = "https://openrouter.ai/api/v1"
MAX_UPLOAD = 50 * 1024 * 1024                   # per file
MAX_NATIVE = 20 * 1024 * 1024                   # larger files are described, not sent inline
UPLOADS = f"{CODE_FOLDER}/uploads"
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".html",
            ".css", ".xml", ".sql", ".sh", ".log", ".ini", ".cfg", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".rb"}
AUDIO_FORMATS = {"wav": "wav", "mp3": "mp3", "mpeg": "mp3", "x-wav": "wav", "flac": "flac", "ogg": "ogg", "aac": "aac",
                 "mp4": "m4a", "m4a": "m4a", "x-m4a": "m4a", "aiff": "aiff"}


def kind_of(name: str, mime: str) -> str:
    mime = (mime or mimetypes.guess_type(name)[0] or "").lower()
    ext = Path(name).suffix.lower()
    if mime.startswith("image/") and ext != ".svg":
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf" or ext == ".pdf":
        return "pdf"
    if mime.startswith("text/") or ext in TEXT_EXT:
        return "text"
    return "file"


def safe_name(name: str) -> str:
    base = re.sub(r"[^\w.\- ]+", "_", Path(name or "file").name).strip(" .") or "file"
    return base[:120]


_SHARED: Dict[str, Any] = {"cat": None, "at": 0.0}


class Media:
    def __init__(self, ws: Workspace, state_dir: Path):
        self.ws, self.state_dir = ws, Path(state_dir)
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ OpenRouter
    @staticmethod
    def _key() -> str:
        keys = keys_from_env("OPENROUTER_API_KEY")
        if not keys:
            raise WorkspaceError(400, "no OpenRouter key: add OPENROUTER_API_KEY=... to the .env of Agent Smith")
        return keys[0]

    def _post(self, path: str, body: Dict[str, Any], timeout: float = 120) -> Dict[str, Any]:
        r = httpx.post(API + path, json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"})
        try:
            data = r.json()
        except ValueError:
            raise WorkspaceError(502, f"OpenRouter answered {r.status_code}") from None
        if r.status_code != 200 or data.get("error"):
            msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
            raise WorkspaceError(502, f"OpenRouter: {msg or r.status_code}")
        return data

    def catalog(self) -> Dict[str, Any]:
        return self.shared_catalog()

    @staticmethod
    def shared_catalog(block: bool = True, on_ready=None) -> Dict[str, Any]:
        """The catalog (fetched once a day per process). block=False never waits: it returns what is cached and
        fetches in the background, then calls ``on_ready()`` (the UI reloads the model list)."""
        if os.environ.get("AGENT_SMITH_OFFLINE"):  # tests / CI: no network, text-only capabilities
            return {}
        stale = _SHARED["cat"] is None or time.time() - _SHARED["at"] > 86400
        if stale and not block:
            if not _SHARED.get("fetching"):
                _SHARED["fetching"] = True

                def fetch() -> None:
                    try:
                        Media.shared_catalog(block=True)
                    finally:
                        _SHARED["fetching"] = False
                    if on_ready and _SHARED["cat"]:
                        on_ready()
                threading.Thread(target=fetch, name="openrouter-catalog", daemon=True).start()
            return _SHARED["cat"] or {}
        if stale:
            try:
                data = httpx.get(API + "/models", timeout=10).json()["data"]
            except (httpx.HTTPError, ValueError, KeyError):
                data = []
            cat = {}
            for m in data:
                p, a = m.get("pricing") or {}, m.get("architecture") or {}
                try:
                    price = {"in": round(float(p.get("prompt") or 0) * 1e6, 4), "out": round(float(p.get("completion") or 0) * 1e6, 4)}
                except ValueError:
                    price = None
                cat[m["id"]] = {"in": a.get("input_modalities") or ["text"], "out": a.get("output_modalities") or ["text"], "price": price}
            _SHARED.update(cat=cat, at=time.time() if data else 0.0)
        return _SHARED["cat"]

    def default(self, kind: str) -> str:
        from agent_smith.studio.openrouter import load_picks
        picked = load_picks()["media"].get(kind)  # chosen in the OpenRouter tab
        if picked:
            return picked
        entries = load_models_config().media.get(kind) or []
        if not entries:
            raise WorkspaceError(400, f"no {kind} model configured (config/models.json, media.{kind})")
        return entries[0]["model"]

    # ------------------------------------------------------------------ uploads
    def save_upload(self, name: str, mime: str, data: bytes) -> Dict[str, Any]:
        if not data:
            raise WorkspaceError(400, "empty file")
        if len(data) > MAX_UPLOAD:
            raise WorkspaceError(413, f"file too large (max {MAX_UPLOAD // 1024 // 1024} MB)")
        uid = uuid.uuid4().hex[:10]
        name = safe_name(name)
        rel = f"{UPLOADS}/{uid}-{name}"
        path = self.ws.resolve(rel, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        ignore = self.ws.root / CODE_FOLDER / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# generated by Agent Smith, never committed\n*\n")
        path.write_bytes(data)
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        info = {"id": uid, "name": name, "path": rel, "mime": mime, "size": len(data), "kind": kind_of(name, mime)}
        with self._lock:
            self.uploads[uid] = info
        return info

    def get(self, uid: str) -> Dict[str, Any]:
        info = self.uploads.get(uid)
        if not info:
            raise WorkspaceError(404, "unknown attachment")
        return info

    def file(self, uid: str) -> Path:
        return self.ws.resolve(self.get(uid)["path"])

    # ------------------------------------------------------------------ voice
    def transcribe(self, audio: bytes, fmt: str = "wav", language_hint: str = "", tier: str = "paid") -> str:
        if not audio:
            raise WorkspaceError(400, "empty recording")
        if len(audio) > MAX_NATIVE:
            raise WorkspaceError(413, "recording too long")
        if tier == "free":  # free tier: Groq's Whisper on the free Groq key, never the OpenRouter key
            return self._free_transcribe(audio, fmt, language_hint)
        prompt = ("Transcribe this audio exactly, in the language spoken. Reply with the transcript only, no quotes, "
                  "no comments." + (f" Likely language: {language_hint}." if language_hint else ""))
        data = self._post("/chat/completions", {"model": self.default("transcribe"), "temperature": 0, "messages": [{
            "role": "user", "content": [{"type": "text", "text": prompt},
                                        {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}]}]})
        return (data["choices"][0]["message"].get("content") or "").strip()

    # ------------------------------------------------------------------ free tier (the free keys, never OpenRouter)
    FREE_STT = ("groq", "whisper-large-v3-turbo")
    FREE_PERCEIVE = ("gemini", "gemini-3.5-flash")  # reads images and audio on the free AI Studio key

    @staticmethod
    def _free_provider(name: str):
        from agent_smith.config import load_models_config
        spec = load_models_config().provider_by_name(name)
        keys = keys_from_env(spec.key_env) if spec else []
        return (spec, keys[0]) if spec and keys else (None, None)

    def _free_transcribe(self, audio: bytes, fmt: str, language_hint: str) -> str:
        spec, key = self._free_provider(self.FREE_STT[0])
        if not spec:
            raise WorkspaceError(400, "voice on the Free tier uses Groq's Whisper: add GROQ_API_KEY=... to the .env "
                                      "(or switch the chat to Paid)")
        data = {"model": self.FREE_STT[1], "temperature": "0", "response_format": "json"}
        if len(language_hint) == 2:
            data["language"] = language_hint
        r = httpx.post(spec.base_url.rstrip("/") + "/audio/transcriptions", data=data, timeout=120,
                       files={"file": (f"voice.{fmt}", audio, f"audio/{fmt}")}, headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            raise WorkspaceError(502, f"Groq Whisper answered {r.status_code}: {r.text[:200]}")
        return (r.json().get("text") or "").strip()

    def _free_describe(self, info: Dict[str, Any], ask: str) -> Optional[str]:
        """Image / audio described by the free Gemini key; None when the free tier cannot read this kind."""
        if info["kind"] not in ("image", "audio"):
            return None
        spec, key = self._free_provider(self.FREE_PERCEIVE[0])
        part = self.part(info) if spec else None
        if not part:
            return None
        from agent_smith.llm.client import build_client
        llm = build_client(self.FREE_PERCEIVE[1], spec.base_url, no_fallbacks=True)
        return llm.chat([{"role": "user", "content": [{"type": "text", "text": ask}, part]}], max_tokens=4000).text.strip()

    # ------------------------------------------------------------------ attachments for a chat turn
    def part(self, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """OpenAI-style content part for a model that reads this kind natively (None for text / unknown)."""
        path = self.ws.resolve(info["path"])
        if info["size"] > MAX_NATIVE:
            return None
        b64 = base64.b64encode(path.read_bytes()).decode()
        if info["kind"] == "image":
            return {"type": "image_url", "image_url": {"url": f"data:{info['mime']};base64,{b64}"}}
        if info["kind"] == "audio":
            fmt = AUDIO_FORMATS.get(info["mime"].split("/")[-1].split(";")[0], Path(info["name"]).suffix.lstrip(".").lower() or "wav")
            return {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}
        if info["kind"] == "video":
            return {"type": "video_url", "video_url": {"url": f"data:{info['mime']};base64,{b64}"}}
        if info["kind"] == "pdf":
            return {"type": "file", "file": {"filename": info["name"], "file_data": f"data:application/pdf;base64,{b64}"}}
        return None

    def describe(self, info: Dict[str, Any], tier: str = "paid") -> str:
        """Text for a model that cannot read the attachment: a description / transcript by the perceive model
        (Free tier: the free Gemini key, images and audio only)."""
        part = self.part(info)
        if part is None:
            return f"(too large to read: {info['size'] // 1024 // 1024} MB)"
        ask = {"image": "Describe this image precisely for a software engineer who cannot see it: all visible text verbatim, "
                        "layout, UI elements, errors, numbers.",
               "audio": "Transcribe this audio verbatim, then add one line describing any non-speech content.",
               "video": "Describe this video for someone who cannot watch it: what happens, step by step, with all visible "
                        "or spoken text verbatim.",
               "pdf": "Extract the content of this document as Markdown: headings, text, tables (verbatim where possible)."}
        if tier == "free":
            text = self._free_describe(info, ask[info["kind"]])
            return text if text is not None else (
                f"(not read: on the Free tier only images and audio are described, by the free Gemini key; "
                f"process the file with your tools, or the user can switch the chat to Paid)")
        data = self._post("/chat/completions", {"model": self.default("perceive"), "temperature": 0, "messages": [{
            "role": "user", "content": [{"type": "text", "text": ask[info["kind"]]}, part]}]}, timeout=300)
        return (data["choices"][0]["message"].get("content") or "").strip()

    def for_turn(self, ids: List[str], model: str, provider: str, note, tier: str = "paid") -> tuple:
        """-> (text to add to the prompt, native content parts). ``note(str)`` reports progress to the trace.
        ``tier``: the chat turn's; on "free" nothing goes through the OpenRouter key."""
        if tier == "free":
            caps = ["text", "image", "audio"] if provider == "gemini" else ["text"]  # free Gemini reads them natively
        else:
            caps = self.catalog().get(model, {}).get("in", ["text"])  # by OpenRouter model id; unknown: text only
        need = {"image": "image", "audio": "audio", "video": "video", "pdf": "file"}
        lines, parts = [], []
        for uid in ids:
            info = self.get(uid)
            where = f"{info['path']} ({info['kind']}, {info['size'] // 1024} KB)"
            if info["kind"] in ("text", "file"):
                lines.append(f"- {where}: open it with read_file('{info['path']}') or process it with run_command.")
                continue
            native = need[info["kind"]] in caps and info["size"] <= MAX_NATIVE
            if native:
                part = self.part(info)
                if part:
                    parts.append(part)
                    lines.append(f"- {where}: attached to this message, you can see it directly.")
                    continue
            reader = self.FREE_PERCEIVE[1] + " (free key)" if tier == "free" else self.default("perceive")
            note(f"reading {info['name']} with {reader} (the chat model cannot read {info['kind']})")
            text = self.describe(info, tier)
            lines.append(f"- {where}: described by a multimodal model (you cannot see it directly):\n"
                         + "\n".join("    " + ln for ln in text.splitlines()))
        prompt = ("\n\nThe user attached files (saved in the project; the paths work with your tools):\n" + "\n".join(lines)) if lines else ""
        return prompt, parts
