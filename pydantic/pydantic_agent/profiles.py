"""
Module: profiles.py

Description:
    `ModelProfiles`: the model profiles shared by all three agents (agents/context/models.json):
    which profile to use, with command-line and environment overrides, which model an LM Studio
    server has loaded, and the pydantic-ai model for the chosen profile.
"""
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Optional

import httpx2
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pydantic_agent import MODELS_FILE


class ModelProfiles:
    """
    The model profiles: "default" (a profile name) and "profiles" (by name).
    """

    def __init__(self, models: dict[str, Any]) -> None:
        """
        Wrap model profiles that are already parsed; `load` reads them from the shared file.
        Args:
            models: The models file's contents: "default" and "profiles".
        """
        self.models = models

    @classmethod
    def load(cls, path: Path = MODELS_FILE) -> "ModelProfiles":
        """
        Read the shared model profiles file.
        Args:
            path: The JSON file (default: agents/context/models.json).
        Returns:
            ModelProfiles: The profiles.
        """
        return cls(json.loads(path.read_text(encoding="utf-8")))

    # noinspection DuplicatedCode
    @staticmethod
    def loaded_model(base_url: str, api_key: str = "") -> Optional[str]:
        """
        Ask an LM Studio server which model is loaded (its /api/v0/models lists each model's state).
        Args:
            base_url: The server's OpenAI-compatible base URL, e.g. http://boba:1234/v1.
            api_key: Sent as a bearer token, for servers that check it.
        Returns:
            Optional[str]: The first loaded language model's id, or None when none is loaded or the
                server cannot tell (not LM Studio, unreachable).
        """
        root = base_url.rstrip("/").removesuffix("/v1")
        request = urllib.request.Request(root + "/api/v0/models", headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                models = json.loads(response.read()).get("data", [])
        except (OSError, ValueError):
            return None
        return next((m["id"] for m in models if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")), None)

    # noinspection DuplicatedCode
    def resolve(self, profile: str | None = None, model: str | None = None,
                base_url: str | None = None) -> dict[str, Any]:
        """
        Pick a model profile and apply overrides, the same way as MCPAgent.
        Precedence: explicit arguments (command line), then the environment variables the profile
        names (model_env, base_url_env), then, with "model_auto", the model loaded on the server
        (LM Studio), then the profile's own values. The API key is read only from the profile's
        api_key_env (or its api_key fallback), so a key is never sent to a server it was not
        configured for.
        Args:
            profile: Profile name; None uses the profile named by "default".
            model: Overrides the profile's model.
            base_url: Overrides the profile's base URL.
        Returns:
            dict[str, Any]: name, base_url, model, api_key and timeout.
        Raises:
            ValueError: If the profile does not exist, lacks base_url or model, or its API key is not set.
        """
        profiles = self.models.get("profiles") or {}
        name = profile or self.models.get("default")
        settings = profiles.get(name)
        if not isinstance(settings, dict):
            available = ", ".join(profiles) or "none"
            raise ValueError(f"Unknown model profile '{name}' (available: {available}). Check the models file.")
        missing = [key for key in ("base_url", "model") if not settings.get(key)]
        if missing:
            raise ValueError(f"Model profile '{name}' is missing {', '.join(missing)}.")
        api_key = (os.environ.get(settings.get("api_key_env", ""), "") or settings.get("api_key", "")).strip()
        if not api_key:
            raise ValueError(f"Set {settings.get('api_key_env', 'an API key')} in the environment for the '{name}' profile.")
        base_url = base_url or os.environ.get(settings.get("base_url_env", "")) or settings["base_url"]
        model = model or os.environ.get(settings.get("model_env", "")) \
            or (settings.get("model_auto") and ModelProfiles.loaded_model(base_url, api_key)) or settings["model"]
        return {
            "name": settings.get("name", name),
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "timeout": float(settings.get("timeout", 60)),
        }

    @staticmethod
    def build_model(settings: dict[str, Any]) -> Model:
        """
        Create a model on an OpenAI-compatible server (LM Studio, OpenAI, ...).
        Args:
            settings: A resolved profile, as returned by `resolve`.
        Returns:
            Model: A pydantic-ai chat-completions model.
        """
        http_client = httpx2.AsyncClient(timeout=settings["timeout"])
        provider = OpenAIProvider(base_url=settings["base_url"], api_key=settings["api_key"], http_client=http_client)
        return OpenAIChatModel(settings["model"], provider=provider)
