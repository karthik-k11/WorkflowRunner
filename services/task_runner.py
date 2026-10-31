"""Allowlisted task implementations. User input is configuration, never code."""

import os
import math
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log"}
MAX_FILE_BYTES = 1_000_000


class TaskExecutionError(RuntimeError):
    pass


def _safe_path(root: Path, relative: Any, *, must_exist: bool) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise TaskExecutionError("A relative file path is required.")
    candidate = (root / relative).resolve(strict=must_exist)
    base = root.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise TaskExecutionError("File path is outside the configured file root.") from exc
    return candidate


def _python_operation(config: dict[str, Any]) -> dict[str, Any]:
    operation = config.get("operation", "echo")
    inputs = config.get("inputs", {})
    if not isinstance(inputs, dict):
        raise TaskExecutionError("Python operation inputs must be an object.")
    if operation == "echo":
        return {"value": inputs.get("value", config.get("value"))}
    if operation == "add":
        values = inputs.get("values", config.get("values", []))
        if not isinstance(values, list) or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
            for value in values
        ):
            raise TaskExecutionError("The add operation requires numeric values.")
        total = sum(values)
        if isinstance(total, float) and not math.isfinite(total):
            raise TaskExecutionError("The add operation result is outside the finite numeric range.")
        return {"value": total}
    if operation == "uppercase":
        value = inputs.get("value", config.get("value", ""))
        if not isinstance(value, str):
            raise TaskExecutionError("The uppercase operation requires text.")
        return {"value": value.upper()}
    if operation == "count":
        value = inputs.get("items", config.get("items", []))
        if not isinstance(value, (list, dict, str)):
            raise TaskExecutionError("The count operation requires a list, object, or text.")
        return {"value": len(value)}
    raise TaskExecutionError(f"Python operation '{operation}' is not allowlisted.")


def _file_operation(
    config: dict[str, Any], root: Path, output_limit: int
) -> dict[str, Any]:
    action = config.get("action")
    if action == "exists":
        path = _safe_path(root, config.get("path"), must_exist=False)
        exists = path.exists()
        return {
            "value": exists,
            "exists": exists,
            "path": str(path.relative_to(root.resolve())),
        }
    if action == "read_text":
        path = _safe_path(root, config.get("path"), must_exist=True)
        if path.suffix.lower() not in TEXT_EXTENSIONS or not path.is_file():
            raise TaskExecutionError("Only permitted text files can be inspected.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise TaskExecutionError("Text file exceeds the 1 MB inspection limit.")
        text = path.read_text(encoding="utf-8")
        return {
            "value": text[:output_limit],
            "text": text[:output_limit],
            "truncated": len(text) > output_limit,
        }
    if action == "copy":
        source = _safe_path(root, config.get("source"), must_exist=True)
        destination = _safe_path(root, config.get("destination"), must_exist=False)
        if source.suffix.lower() not in TEXT_EXTENSIONS or not source.is_file():
            raise TaskExecutionError("Only permitted text files can be copied.")
        if source.stat().st_size > MAX_FILE_BYTES:
            raise TaskExecutionError("Source file exceeds the 1 MB copy limit.")
        if source == destination:
            raise TaskExecutionError("Source and destination must be different files.")
        if destination.exists():
            raise TaskExecutionError("Copy destination already exists.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return {
            "value": True,
            "copied": True,
            "destination": str(destination.relative_to(root.resolve())),
        }
    raise TaskExecutionError(f"File action '{action}' is not supported.")


def _command_operation(
    config: dict[str, Any], allowed_commands: dict[str, list[str]], timeout: int, output_limit: int
) -> dict[str, Any]:
    command_key = config.get("command")
    command = allowed_commands.get(command_key)
    if command is None:
        raise TaskExecutionError(f"Command '{command_key}' is not allowlisted.")
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) for part in command
    ):
        raise TaskExecutionError("Configured command must be a fixed argument list.")
    executable = command[0]
    if executable == "git":
        resolved = shutil.which(executable)
        if resolved is None:
            raise TaskExecutionError("The allowlisted git executable is not installed.")
        command = [resolved, *command[1:]]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        | {"PYTHONIOENCODING": "utf-8"},
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise TaskExecutionError(
            f"Command exceeded its {timeout}-second timeout."
        ) from exc
    combined = stdout + (("\n" if stdout and stderr else "") + stderr)
    truncated = len(combined) > output_limit
    combined = combined[:output_limit]
    if process.returncode:
        raise TaskExecutionError(
            f"Command exited with code {process.returncode}: {combined}"
        )
    return {
        "value": combined,
        "returncode": process.returncode,
        "output": combined,
        "truncated": truncated,
    }


def execute_task(
    task_type: str,
    config: dict[str, Any],
    file_root: Path,
    allowed_commands: dict[str, list[str]],
    timeout: int,
    output_limit: int,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        if task_type == "python":
            result = _python_operation(config)
        elif task_type == "file":
            result = _file_operation(config, file_root, output_limit)
        elif task_type == "command":
            result = _command_operation(config, allowed_commands, timeout, output_limit)
        else:
            raise TaskExecutionError(f"Task type '{task_type}' is not supported.")
    except OSError as exc:
        raise TaskExecutionError(f"Task operation failed: {exc}") from exc
    result["elapsed_seconds"] = round(time.monotonic() - started, 4)
    return result
