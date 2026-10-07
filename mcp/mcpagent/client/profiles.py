"""
Module: profiles.py

Description:
    `ModelProfiles`: the model profiles shared by all three agents (context/models.json, which the
    client config's "models_file" names): which profile to use, with command-line and environment
    overrides, and, for an LM Studio server, which model is loaded.
"""
import json
import os
import urllib.request
from typing import Any, Optional

from mcpagent.config import MCPAgentConfig


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
    def load(cls, client_config: dict[str, Any]) -> "ModelProfiles":
        """
        Read the model profiles from the file the client config names.
        Args:
            client_config: The client config; its "models_file" is relative to the repository.
        Returns:
            ModelProfiles: The profiles.
        Raises:
            ValueError: If the config does not name a models file.
        """
        models_file: Optional[str] = client_config.get("models_file")
        if not models_file:
            raise ValueError('The client config has no "models_file"; point it at context/models.json.')
        path = MCPAgentConfig.repo_path(models_file)
        return cls(json.loads(path.read_text(encoding="utf-8")))

    # Match model discovery across the independent agent implementations.
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

    # Keep profile precedence consistent across the independent agent implementations.
    # noinspection DuplicatedCode
    def resolve(self, profile: Optional[str] = None, model: Optional[str] = None,
                base_url: Optional[str] = None) -> dict[str, Any]:
        """
        Pick a model profile and apply overrides.
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
            dict[str, Any]: name, base_url, model, api_key, timeout and error_hints, as `MCPAgent`
                expects.
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
            "error_hints": settings.get("error_hints"),
        }
