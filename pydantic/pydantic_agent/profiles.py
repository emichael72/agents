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
from pydantic_ai.settings import ModelSettings

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
            dict[str, Any]: profile (its name), name, base_url, model, api_key, timeout, max_tokens
                (None when the profile sets none) and sampling ({} when none).
        Raises:
            ValueError: If the profile does not exist, lacks base_url or model, or its API key is not set.
        """
        profiles = self.models.get("profiles") or {}
        name = profile or self.models.get("default")
        settings = profiles.get(name)
        if not isinstance(settings, dict):
            available = ", ".join(profiles) or "none"
            raise ValueError(f"Unknown model profile '{name}' (available: {available}). Check the models file.")
        # With model_auto the model may be left out: the one loaded on the server is used
        required = ("base_url",) if settings.get("model_auto") else ("base_url", "model")
        missing = [key for key in required if not settings.get(key)]
        if missing:
            raise ValueError(f"Model profile '{name}' is missing {', '.join(missing)}.")
        api_key = (os.environ.get(settings.get("api_key_env", ""), "") or settings.get("api_key", "")).strip()
        if not api_key:
            raise ValueError(f"Set {settings.get('api_key_env', 'an API key')} in the environment for the '{name}' profile.")
        base_url = base_url or os.environ.get(settings.get("base_url_env", "")) or settings["base_url"]
        model = model or os.environ.get(settings.get("model_env", "")) \
            or (settings.get("model_auto") and ModelProfiles.loaded_model(base_url, api_key)) or settings.get("model")
        if not model:  # Never ask the server to load a model it has not loaded
            raise ValueError(f"No model is loaded on {base_url}, and the '{name}' profile names none (it uses "
                             f"the loaded model): load one in LM Studio, or name one with --model or {settings.get('model_env') or 'model in the profile'}.")
        return {
            "profile": name,
            "name": settings.get("name", name),
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "timeout": float(settings.get("timeout", 60)),
            "max_tokens": int(settings["max_tokens"]) if settings.get("max_tokens") else None,
            "sampling": dict(settings.get("sampling") or {}),  # Sent as they are with each model call
        }

    @staticmethod
    def build_model(settings: dict[str, Any]) -> Model:
        """
        Create a model on an OpenAI-compatible server (LM Studio, OpenAI, ...), limited to the
        profile's max_tokens per reply when it sets one, with its sampling settings in each request.
        Args:
            settings: A resolved profile, as returned by `resolve`.
        Returns:
            Model: A pydantic-ai chat-completions model.
        """
        http_client = httpx2.AsyncClient(timeout=settings["timeout"])
        provider = OpenAIProvider(base_url=settings["base_url"], api_key=settings["api_key"], http_client=http_client)
        model_settings = ModelSettings()
        if settings.get("max_tokens"):
            model_settings["max_tokens"] = settings["max_tokens"]
        if settings.get("sampling"):  # As they are, top_k and min_p too, which ModelSettings does not name
            model_settings["extra_body"] = settings["sampling"]
        return OpenAIChatModel(settings["model"], provider=provider, settings=model_settings or None)

    @staticmethod
    def share_with_tools(settings: dict[str, Any]) -> None:
        """
        Put the model in the environment the tools inherit, for the sysinfo tool's model section
        (the same in all three agents): its id, server, profile, max_tokens and sampling; never the key.
        Args:
            settings: The resolved profile.
        """
        os.environ.update({"AGENT_MODEL": settings["model"], "AGENT_MODEL_SERVER": settings["base_url"],
                           "AGENT_MODEL_PROFILE": settings["profile"],
                           "AGENT_MAX_TOKENS": str(settings.get("max_tokens") or ""),
                           "AGENT_SAMPLING": json.dumps(settings.get("sampling") or {})})

    @staticmethod
    def out_of_tokens(settings: dict[str, Any]) -> str:
        """
        What to tell the user when a reply hit the profile's max_tokens (the same in all three agents).
        Args:
            settings: The resolved profile.
        Returns:
            str: The message.
        """
        limit = f"{settings['max_tokens']:,}" if settings.get("max_tokens") else "the server's limit of"
        return (f"The model ran out of tokens: it may use {limit} tokens per reply, thinking included "
                f"(max_tokens in the '{settings.get('profile')}' profile, context/models.json). Raise it, or ask "
                f"for a smaller step.")
