"""
Module: generator.py

Description:
    `QuizGenerator`: has a model write the developer quiz for a pull request's diff, through the
    model profiles shared with the agents (context/models.json) and the OpenAI-compatible chat
    completions API, which both LM Studio and OpenAI serve. The instructions are
    gatekeepers/pr/context/instructions.json, read per quiz so edits apply without a restart.
"""

import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from gatekeepers.pr import INSTRUCTIONS_FILE, MODELS_FILE
from gatekeepers.pr.quiz import Question, Quiz
from gatekeepers.pr.settings import GateSettings


class QuizGenerator:
    """
    Writes quizzes with the model of one profile.
    """

    QUESTIONS_KEPT = 3  # The developer's quiz; the writer drafts one more, in case the answer check drops one

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
        self.verify_answers = GateSettings.setting("QUIZ_VERIFY_ANSWERS", required=False).lower() in ("true", "1", "yes")
        self.verify_profile = GateSettings.setting("QUIZ_VERIFY_PROFILE", required=False) or None  # None: the writer's
        self.attempts = int(GateSettings.setting("QUIZ_MODEL_ATTEMPTS"))
        if self.attempts < 1:
            raise ValueError("QUIZ_MODEL_ATTEMPTS must be at least 1.")

    def instructions(self, key: str = "instructions") -> str:
        """
        Read the quiz writer's instructions, or the answer checker's.
        Args:
            key: "instructions" (writing the quiz) or "verify" (answering its questions again).
        Returns:
            str: Those lines, joined with newlines.
        """
        return "\n".join(json.loads(self.instructions_file.read_text(encoding="utf-8"))[key])

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
        # With model_auto the model may be left out: the one loaded on the server is used
        required = ("base_url",) if settings.get("model_auto") else ("base_url", "model")
        missing = [key for key in required if not settings.get(key)]
        if missing:
            raise ValueError(f"Model profile '{name}' is missing {', '.join(missing)}.")
        api_key = (os.environ.get(settings.get("api_key_env", ""), "") or settings.get("api_key", "")).strip()
        if not api_key:
            raise ValueError(f"Set {settings.get('api_key_env', 'an API key')} in the environment for the '{name}' profile.")
        base_url = os.environ.get(settings.get("base_url_env", "")) or settings["base_url"]
        model = os.environ.get(settings.get("model_env", "")) \
            or (settings.get("model_auto") and self.loaded_model(base_url, api_key)) or settings.get("model")
        if not model:  # Never ask the server to load a model it has not loaded
            raise ValueError(f"No model is loaded on {base_url}, and the '{name}' profile names none (it uses "
                             f"the loaded model): load one in LM Studio, or name one with {settings.get('model_env') or 'model in the profile'}.")
        return {
            "name": settings.get("name", name),
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "timeout": float(settings.get("timeout", 60)),
            "max_tokens": int(settings["max_tokens"]) if settings.get("max_tokens") else None,
            "profile": name,
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

    @staticmethod
    def unfinished(reason: Optional[str], usage: dict[str, Any], settings: dict[str, Any]) -> str:
        """
        Say why a reply ended before the quiz was finished, and what to change.
        Args:
            reason: The reply's finish_reason, e.g. "length".
            usage: The reply's token usage, as the server reports it.
            settings: The model profile the request used (`resolve_model`).
        Returns:
            str: The reason, for the error message.
        """
        if reason != "length":
            return f"the model stopped before finishing the quiz ({reason or 'no reason given'})"
        used = usage.get("completion_tokens") or settings.get("max_tokens")
        thinking = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        spent = (f" all {used:,}" if used else "") + (f", {thinking:,} of them thinking" if thinking else "")
        return (f"the model ran out of tokens: it used{spent}; raise max_tokens in the "
                f"'{settings.get('profile')}' profile in context/models.json")

    def generate(self, diff: str, profile: Optional[str] = None,
                 code_files: Optional[list[str]] = None, context: str = "") -> tuple[Quiz, str]:
        """
        Ask the model for a quiz about a diff, up to QUIZ_MODEL_ATTEMPTS times. Invalid or unfinished
        replies are retried within that limit, as are replies that call a code change cosmetic.
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
        for _ in range(self.attempts):
            content, error = self.complete(settings, instructions, prompt)
            if content is None:
                continue
            try:
                quiz = self.parse(content)
            except ValidationError as exc:
                error = f"the reply was not a valid quiz ({exc.error_count()} errors)"
                continue
            if quiz.cosmetic and code_files:
                error = "the model called a code change cosmetic"
                continue
            if not quiz.cosmetic and self.verify_answers:
                confirmed, error = self.verify(quiz, prompt, settings)
                if error:
                    continue
                if len(confirmed) < self.QUESTIONS_KEPT:
                    error = (f"a second reading confirmed the answers of only {len(confirmed)} of "
                             f"{len(quiz.questions)} questions")
                    continue
                quiz.questions = confirmed
            quiz.questions = quiz.questions[:self.QUESTIONS_KEPT]
            return quiz, f"{settings['name']} / {settings['model']}"
        raise ValueError(f"No quiz was created: {error}.")

    def complete(self, settings: dict[str, Any], system: str, user: str,
                 temperature: float = 0.2) -> tuple[Optional[str], Optional[str]]:
        """
        One chat completion on the profile's server.
        Args:
            settings: The model profile (`resolve_model`).
            system: The instructions.
            user: The message.
            temperature: Sampling temperature; the answer check uses 0.
        Returns:
            tuple[Optional[str], Optional[str]]: The reply's text and None; or None and why there is no
                usable reply (it did not finish).
        Raises:
            httpx.HTTPError: If the model server cannot be reached or rejects the request.
        """
        response = httpx.post(
            settings["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + settings["api_key"]},
            json={"model": settings["model"], "temperature": temperature,
                  **({"max_tokens": settings["max_tokens"]} if settings.get("max_tokens") else {}),
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=settings["timeout"])
        response.raise_for_status()
        reply = response.json()
        choice = reply["choices"][0]
        if choice.get("finish_reason") != "stop":
            return None, self.unfinished(choice.get("finish_reason"), reply.get("usage") or {}, settings)
        return choice["message"]["content"] or "", None

    def verify(self, quiz: Quiz, prompt: str, settings: dict[str, Any]) -> tuple[list[Question], Optional[str]]:
        """
        Have the model answer the quiz's questions again, from the same diff but without the marked
        answers, quoting for each answer the line of the diff that proves it, and keep the questions
        whose answer it confirms with a quote that is really in the diff: a wrong answer key would
        fail a developer who read the code correctly. (QUIZ_VERIFY_ANSWERS turns this off.)
        Args:
            quiz: The quiz as written.
            prompt: The message the quiz was written from (context and diff).
            settings: The writer's model profile; the checker uses QUIZ_VERIFY_PROFILE's when it is set.
        Returns:
            tuple[list[Question], Optional[str]]: The confirmed questions, in order, and None; or [] and
                why the check could not be made.
        """
        listing = "\n\n".join(f"Question {n}: {q.question}\n" + "\n".join(f"  {i}) {option}" for i, option in enumerate(q.options))
                               for n, q in enumerate(quiz.questions, 1))
        checker = self.resolve_model(self.verify_profile) if self.verify_profile else settings
        content, error = self.complete(checker, self.instructions("verify"), f"{prompt}\n\nQuestions:\n{listing}",
                                       temperature=0)
        if content is None:
            return [], f"the answer check failed: {error}"
        fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", content, re.DOTALL)
        try:
            answers = json.loads(fenced.group(1) if fenced else content)["answers"]
            if not isinstance(answers, list) or len(answers) != len(quiz.questions) or \
                    not all(isinstance(a, dict) and isinstance(a.get("answer"), int) for a in answers):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return [], "the answer check's reply was not a list of answers"
        source = " ".join(prompt.split())  # The quote must be in what the checker was given, spacing aside
        confirmed = [q for q, answer in zip(quiz.questions, answers, strict=True)
                     if answer["answer"] == q.correct and self.quoted(answer.get("evidence"), source)]
        logging.getLogger("pr_gate").info("Quiz answers checked by %s: %s of %s confirmed", checker["model"],
                                          len(confirmed), len(quiz.questions))
        return confirmed, None

    @staticmethod
    def quoted(evidence: Any, source: str) -> bool:
        """
        Whether the checker's evidence is a real quote: nonempty, and in the message it was given (the
        diff's + and - markers and spacing aside), so an answer cannot rest on code that is not there.
        Args:
            evidence: The checker's quote.
            source: The message the checker answered from, its spacing evened out.
        Returns:
            bool: True for a real quote.
        """
        if not isinstance(evidence, str):
            return False
        lines = [" ".join(line.strip().lstrip("+-").split()) for line in evidence.splitlines()]
        lines = [line for line in lines if line]
        return bool(lines) and all(line in source for line in lines)
