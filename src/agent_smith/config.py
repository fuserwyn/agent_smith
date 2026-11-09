"""Configuration helpers: .env loading, sandbox config files, provider registry.

API keys are NEVER stored in code or in config files: config files only name
the environment variables that hold them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from agent_smith.models import SandboxConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Optional[str | os.PathLike] = None, override: bool = False) -> List[str]:
    """Load ``KEY=VALUE`` lines from a .env file into ``os.environ``.

    Supports comments, blank lines, ``export KEY=...`` and single/double quotes.
    Returns the list of keys that were set. Missing file -> no-op.
    """
    candidates = [Path(path)] if path else [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    loaded: List[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if override or key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
        break
    return loaded


def load_sandbox_config(path: Optional[str | os.PathLike] = None) -> SandboxConfig:
    """Load a :class:`SandboxConfig` from JSON (defaults when ``path`` is None)."""
    if not path:
        return SandboxConfig()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return SandboxConfig.model_validate(data)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
class ProviderSpec(BaseModel):
    """A provider entry from ``config/models.json``."""

    name: str
    base_url: str
    key_env: Optional[str] = Field(
        default=None, description="Env-var prefix holding keys, e.g. OPENROUTER_API_KEY (None = keyless)"
    )
    hosts: List[str] = Field(default_factory=list, description="Hostnames that identify this provider")
    extra_headers: Dict[str, str] = Field(default_factory=dict)


class ModelSpec(BaseModel):
    provider: str
    model: str
    notes: str = ""


class ModelsConfig(BaseModel):
    providers: List[ProviderSpec] = Field(default_factory=list)
    models: List[ModelSpec] = Field(default_factory=list)
    fallbacks: List[ModelSpec] = Field(default_factory=list)

    def provider_by_name(self, name: str) -> Optional[ProviderSpec]:
        return next((p for p in self.providers if p.name == name), None)

    def provider_for_url(self, url: str) -> Optional[ProviderSpec]:
        host = (urlparse(url).hostname or "").lower()
        for spec in self.providers:
            if any(host == h or host.endswith("." + h) for h in spec.hosts):
                return spec
        return None


def load_models_config(path: Optional[str | os.PathLike] = None) -> ModelsConfig:
    path = Path(path) if path else PROJECT_ROOT / "config" / "models.json"
    if not path.is_file():
        return ModelsConfig()
    return ModelsConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def keys_from_env(prefix: Optional[str]) -> List[str]:
    """Collect every API key for a provider.

    Accepted forms (all may be combined, duplicates removed, order kept)::

        OPENROUTER_API_KEY=k1
        OPENROUTER_API_KEY_2=k2 ... OPENROUTER_API_KEY_9=k9
        OPENROUTER_API_KEYS=k3,k4
    """
    if not prefix:
        return []
    found: List[str] = []
    candidates = [os.environ.get(prefix, "")]
    candidates += [os.environ.get(f"{prefix}_{i}", "") for i in range(1, 10)]
    candidates += os.environ.get(f"{prefix}S", "").split(",")
    for value in candidates:
        value = value.strip()
        if value and value not in found:
            found.append(value)
    return found
