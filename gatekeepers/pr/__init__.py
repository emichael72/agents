"""
Module: __init__.py

Description:
    The pull request gate package. Run it with pr_gate.sh, which uses the shared .venv:
    python -m gatekeepers.pr.pr_gate. Its modules:
      - pr_gate: the command line; server: the web service and the GitHub poller.
      - gate: QuizGate, which assesses a pull request revision and posts the result.
      - settings, store, github, generator and quiz: its settings, SQLite storage, the GitHub
        CLI, the model that writes the quiz, and the quiz's shape.
      - changes and clone: what a pull request changes, and the local clone kept current.

    It holds the files the gate shares; the classes live in their modules.
"""
from gatekeepers import CONTEXT_DIR, GATEKEEPERS_DIR

GATE_DIR = GATEKEEPERS_DIR / "pr"
SETTINGS_FILE = GATE_DIR / "settings.json"  # The service's and the pr_gate tool's settings
INSTRUCTIONS_FILE = GATE_DIR / "context" / "instructions.json"  # The quiz writer's instructions
TEMPLATES_DIR = GATE_DIR / "templates"  # The web pages
DATA_DIR = GATE_DIR / "data"  # The quiz database and the secret key; QUIZ_DATA_DIR overrides
MODELS_FILE = CONTEXT_DIR / "models.json"  # The model profiles shared with the agents
