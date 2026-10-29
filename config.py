"""Application configuration for the local WorkflowRunner instance."""

from pathlib import Path
import os
import sys


BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("WORKFLOWRUNNER_SECRET_KEY") or os.urandom(32)
    DATABASE = os.environ.get(
        "WORKFLOWRUNNER_DATABASE", str(BASE_DIR / "instance" / "workflowrunner.sqlite3")
    )
    FILE_ROOT = os.environ.get(
        "WORKFLOWRUNNER_FILE_ROOT", str(BASE_DIR / "instance" / "workflow-files")
    )
    MAX_CONTENT_LENGTH = 1024 * 1024
    OUTPUT_LIMIT = 16_384
    MAX_TASK_TIMEOUT = 300
    MAX_RETRIES = 5
    MAX_PARALLEL_TASKS = 4
    ALLOWED_COMMANDS = {
        "python-version": [sys.executable, "--version"],
        "git-version": ["git", "--version"],
    }
