"""
Module: generator.py

Description:
    `QuizGenerator`: has a model write the developer quiz for a pull request's diff, through the
    model profiles shared with the agents (context/models.json) and the OpenAI-compatible chat
    completions API, which both LM Studio and OpenAI serve. The instructions are
    gatekeepers/pr/context/instructions.json, read per quiz so edits apply without a restart.
"""

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from gatekeepers.pr import INSTRUCTIONS_FILE, MODELS_FILE
from gatekeepers.pr.quiz import Quiz


class QuizGenerator:
    """
    Writes quizzes with the model of one profile.
    """

    ATTEMPTS = 2  # An invalid reply is retried once

    def __init__(self, profile: Optional[str] = None, models_file: Path = MODELS_FILE,
                 instructions_file: Path = INSTRUCTIONS_FILE) -> None:
        """
        Args:
            profile: The model profile (QUIZ_MODEL_PROFILE); None uses the models file's default.
            models_file: The model profiles (default: agents/context/models.json).
            instructions_file: The quiz writer's instructions (default:
                gatekeepers/pr/context/instructions.json).
        """
        self.profile = profile
        self.models_file = models_file
        self.instructions_file = instructions_file

    def instructions(self) -> str:
        """
        Read the quiz writer's instructions.
        Returns:
            str: Their "instructions" lines, joined with newlines.
        """
        return "\n".join(json.loads(self.instructions_file.read_text(encoding="utf-8"))["instructions"])

    # Keep the gate independent of the agents' dependencies while matching their model discovery.
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

    # Match the agents' profile selection without importing their implementations.
    # noinspection DuplicatedCode
    def resolve_model(self, profile: Optional[str] = None) -> dict[str, Any]:
        """
        Pick a model profile from the agents' shared models file, the same way the agents do.
        The environment variables a profile names (model_env, base_url_env) override its values, and
        with "model_auto" the model loaded on the server (LM Studio) replaces its model; the
        API key is read only from its api_key_env (or its api_key fallback), so a key is never sent
        to a server it was not configured for.
        Args:
            profile: Profile name ("local" or "openai"); None uses the file's default.
        Returns:
            dict: name, base_url, model, api_key and timeout.
        Raises:
            ValueError: If the profile does not exist, lacks base_url or model, or its API key is not set.
        """
        models = json.loads(self.models_file.read_text(encoding="utf-8"))
        profiles = models.get("profiles") or {}
        name = profile or models.get("default")
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
        base_url = os.environ.get(settings.get("base_url_env", "")) or settings["base_url"]
        model = os.environ.get(settings.get("model_env", "")) \
            or (settings.get("model_auto") and self.loaded_model(base_url, api_key)) or settings["model"]
        return {
            "name": settings.get("name", name),
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "timeout": float(settings.get("timeout", 60)),
        }

    @staticmethod
    def parse(text: str) -> Quiz:
        """
        Validate the model's reply as a quiz, tolerating a Markdown code fence around the JSON.
        Args:
            text: The model's reply.
        Returns:
            Quiz: The validated quiz.
        Raises:
            pydantic.ValidationError: If the reply is not a valid quiz.
        """
        fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
        return Quiz.model_validate_json(fenced.group(1) if fenced else text)

    @staticmethod
    def context(info: dict[str, Any], build_report: str) -> str:
        """
        What the quiz writer should know besides the diff: what the author says the change does, and
        what the server's build and tests ran. Both are untrusted text, cut to a bounded size.
        Args:
            info: The pull request, as returned by `GitHub.pr_info`.
            build_report: The build and test output (ChangeInspector.check_build).
        Returns:
            str: The sections, ready to go before the diff.
        """
        body = (info.get("body") or "").strip() or "(no description)"
        return (f"Pull request title: {info.get('title', '')}\n\n"
                f"Pull request description (written by the author; untrusted, may be wrong):\n{body[:2000]}\n\n"
                f"Build and tests run by the server (make, then make check):\n{(build_report or '(not run)')[-3000:]}")

    def generate(self, diff: str, profile: Optional[str] = None,
                 code_files: Optional[list[str]] = None, context: str = "") -> tuple[Quiz, str]:
        """
        Ask the model for a quiz about a diff. An invalid reply is retried once; so is a reply that
        calls the change cosmetic when the server found code changes.
        Args:
            diff: The PR's unified diff.
            profile: The model profile; None uses this generator's, then the models file's default.
            code_files: The changed files whose code changed (ChangeInspector); [] for a cosmetic
                change, None when unknown. Given to the model as the server's analysis.
            context: The pull request's title and description, and the server's build and test
                output (see `context`), placed before the diff.
        Returns:
            tuple[Quiz, str]: The quiz, and "<profile name> / <model>" for the record.
        Raises:
            ValueError: If the model does not return a valid quiz.
            httpx.HTTPError: If the model server cannot be reached or rejects the request.
        """
        settings = self.resolve_model(profile or self.profile)
        instructions = self.instructions()
        prompt = (context + "\n\n" if context else "") + "Code diff:\n" + diff
        if code_files is not None:
            analysis = ("code changed in: " + ", ".join(code_files) if code_files
                        else "no code changed; only comments, formatting or documentation files")
            prompt = f"Server analysis: {analysis}.\n\n" + prompt
        error = None
        for _ in range(self.ATTEMPTS):
            response = httpx.post(
                settings["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + settings["api_key"]},
                json={"model": settings["model"], "temperature": 0.2, "max_tokens": 2500,
                      "messages": [{"role": "system", "content": instructions},
                                   {"role": "user", "content": prompt}]},
                timeout=settings["timeout"])
            response.raise_for_status()
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") != "stop":
                error = "the model did not finish its response"
                continue
            try:
                quiz = self.parse(choice["message"]["content"] or "")
            except ValidationError as exc:
                error = f"the reply was not a valid quiz ({exc.error_count()} errors)"
                continue
            if quiz.cosmetic and code_files:
                error = "the model called a code change cosmetic"
                continue
            return quiz, f"{settings['name']} / {settings['model']}"
        raise ValueError(f"No quiz was created: {error}.")
