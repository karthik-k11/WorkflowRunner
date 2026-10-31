"""Dependency-aware execution with bounded concurrency and durable history."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from database import db_session
from models.task import Task
from models.workflow import Workflow
from services.task_runner import TaskExecutionError, execute_task
from services.workflow_store import now_iso
from services.workflow_validator import resolve_execution_order


def _resolve_value(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        if "from_task" in value:
            task_id = value["from_task"]
            if task_id not in outputs:
                raise TaskExecutionError(f"Output for task '{task_id}' is unavailable.")
            result: Any = outputs[task_id]
            for part in str(value.get("path", "")).split("."):
                if part:
                    if not isinstance(result, dict) or part not in result:
                        raise TaskExecutionError(
                            f"Output path '{value.get('path')}' is unavailable."
                        )
                    result = result[part]
            return result
        return {key: _resolve_value(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, outputs) for item in value]
    return value


def _condition_matches(condition: Any, outputs: dict[str, dict[str, Any]]) -> bool:
    if condition is None:
        return True
    if not isinstance(condition, dict):
        raise TaskExecutionError("Task condition must be an object.")
    operator = condition.get("operator", "truthy")
    left = _resolve_value(condition.get("left"), outputs)
    if operator == "truthy":
        return bool(left)
    if operator == "falsy":
        return not bool(left)
    right = _resolve_value(condition.get("right"), outputs)
    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    raise TaskExecutionError(f"Condition operator '{operator}' is not supported.")


def _run_one(
    task: Task,
    outputs: dict[str, dict[str, Any]],
    file_root: Path,
    allowed_commands: dict[str, list[str]],
    output_limit: int,
    max_retries: int,
) -> tuple[str, dict[str, Any] | None, str | None, str]:
    try:
        config = dict(task.config)
        if not _condition_matches(config.get("condition"), outputs):
            return "skipped", None, None, now_iso()
        config["inputs"] = _resolve_value(config.get("inputs", {}), outputs)
        retry = config.get("retry", {})
        if not isinstance(retry, dict):
            raise TaskExecutionError("Retry policy must be an object.")
        attempts = retry.get("attempts", 1)
        delay = retry.get("delay_seconds", 0)
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 1
            or attempts > max_retries
        ):
            raise TaskExecutionError(
                f"Retry attempts must be between 1 and {max_retries}."
            )
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or (isinstance(delay, float) and not math.isfinite(delay))
            or delay < 0
            or delay > 5
        ):
            raise TaskExecutionError("Retry delay must be between 0 and 5 seconds.")
        last_error = "Task failed."
        for attempt in range(attempts):
            try:
                result = execute_task(
                    task.task_type,
                    config,
                    file_root,
                    allowed_commands,
                    task.timeout_seconds,
                    output_limit,
                )
                return "success", result, None, now_iso()
            except TaskExecutionError as exc:
                last_error = str(exc)
                if attempt + 1 < attempts and delay:
                    time.sleep(delay)
        return "failed", None, last_error, now_iso()
    except (TaskExecutionError, TypeError, ValueError) as exc:
        return "failed", None, str(exc), now_iso()


def execute_workflow(
    database: str | Path,
    workflow: Workflow,
    tasks: list[Task],
    file_root: Path,
    allowed_commands: dict[str, list[str]],
    *,
    output_limit: int = 16_384,
    max_retries: int = 5,
    max_parallel: int = 1,
    cancel_event: threading.Event | None = None,
    execution_id: str | None = None,
) -> str:
    """Run a workflow and persist each execution/task state transition.

    Failure policy: independent branches continue. Tasks that depend on a
    failed, blocked, skipped, or cancelled prerequisite become blocked.
    """
    ordered = resolve_execution_order(tasks)
    if isinstance(max_parallel, bool) or not 1 <= max_parallel <= 16:
        raise ValueError("Concurrency must be between 1 and 16.")
    identifier = execution_id or str(uuid4())
    started = datetime.now(timezone.utc)
    started_iso = started.isoformat()
    with db_session(database) as connection:
        queued = connection.execute(
            "SELECT status FROM executions WHERE execution_id = ?", (identifier,)
        ).fetchone()
        if queued and queued["status"] == "queued":
            connection.execute(
                "UPDATE executions SET status = 'running', started_at = ? WHERE execution_id = ?",
                (started_iso, identifier),
            )
        else:
            connection.execute(
                """INSERT INTO executions
                   (execution_id, workflow_id, status, started_at)
                   VALUES (?, ?, 'running', ?)""",
                (identifier, workflow.workflow_id, started_iso),
            )
        for task in ordered:
            connection.execute(
                """INSERT INTO execution_tasks
                   (execution_id, task_id, task_name, status)
                   VALUES (?, ?, ?, 'pending')""",
                (identifier, task.task_id, task.name),
            )

    states: dict[str, str] = {}
    outputs: dict[str, dict[str, Any]] = {}
    pending = {task.task_id: task for task in ordered}

    def update_task(
        task: Task,
        status: str,
        task_started: str | None,
        task_ended: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        duration = None
        if task_started:
            duration = max(
                0.0,
                (
                    datetime.fromisoformat(task_ended)
                    - datetime.fromisoformat(task_started)
                ).total_seconds(),
            )
        with db_session(database) as connection:
            connection.execute(
                """UPDATE execution_tasks
                   SET status = ?, started_at = ?, ended_at = ?,
                       duration_seconds = ?, output_json = ?, error = ?
                   WHERE execution_id = ? AND task_id = ?""",
                (
                    status,
                    task_started,
                    task_ended,
                    duration,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    identifier,
                    task.task_id,
                ),
            )

    execution_error: str | None = None
    try:
        file_root.mkdir(parents=True, exist_ok=True)
        while pending:
            if cancel_event is not None and cancel_event.is_set():
                for task in pending.values():
                    states[task.task_id] = "cancelled"
                    update_task(task, "cancelled", None, now_iso())
                pending.clear()
                break

            changed = False
            for task_id, task in list(pending.items()):
                dependency_states = [states.get(dep) for dep in task.dependencies]
                if any(state in {"failed", "blocked", "skipped", "cancelled"} for state in dependency_states):
                    states[task_id] = "blocked"
                    update_task(task, "blocked", None, now_iso(), error="A prerequisite did not succeed.")
                    del pending[task_id]
                    changed = True
                elif not task.enabled:
                    states[task_id] = "skipped"
                    update_task(task, "skipped", None, now_iso())
                    del pending[task_id]
                    changed = True
            if changed:
                continue

            ready = sorted(
                (
                    task
                    for task in pending.values()
                    if all(states.get(dep) == "success" for dep in task.dependencies)
                ),
                key=lambda task: task.task_id,
            )
            if not ready:
                raise RuntimeError("No runnable task remains in a validated dependency graph.")

            task_started_times: dict[str, str] = {}
            for task in ready:
                states[task.task_id] = "running"
                task_started = now_iso()
                task_started_times[task.task_id] = task_started
                with db_session(database) as connection:
                    connection.execute(
                        """UPDATE execution_tasks SET status = 'ready'
                           WHERE execution_id = ? AND task_id = ?""",
                        (identifier, task.task_id),
                    )
                    connection.execute(
                        """UPDATE execution_tasks SET status = 'running', started_at = ?
                           WHERE execution_id = ? AND task_id = ?""",
                        (task_started, identifier, task.task_id),
                    )

            results: dict[str, tuple[str, dict[str, Any] | None, str | None, str]] = {}
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(ready))) as pool:
                futures = {
                    pool.submit(
                        _run_one,
                        task,
                        dict(outputs),
                        file_root,
                        allowed_commands,
                        output_limit,
                        max_retries,
                    ): task
                    for task in ready
                }
                for future in as_completed(futures):
                    task = futures[future]
                    results[task.task_id] = future.result()

            for task in ready:
                status, result, error, ended = results[task.task_id]
                states[task.task_id] = status
                if status == "success" and result is not None:
                    outputs[task.task_id] = result
                update_task(
                    task,
                    status,
                    task_started_times[task.task_id],
                    ended,
                    result=result,
                    error=error,
                )
                del pending[task.task_id]

        if any(state in {"failed", "blocked"} for state in states.values()):
            final_status = "failed"
        elif "cancelled" in states.values():
            final_status = "cancelled"
        else:
            final_status = "success"
    except Exception as exc:
        final_status = "failed"
        execution_error = str(exc)[:output_limit]
        with db_session(database) as connection:
            connection.execute(
                """UPDATE execution_tasks SET status = 'failed', ended_at = ?, error = ?
                   WHERE execution_id = ? AND status IN ('pending', 'running')""",
                (now_iso(), str(exc)[:output_limit], identifier),
            )

    ended = datetime.now(timezone.utc)
    with db_session(database) as connection:
        connection.execute(
            """UPDATE executions SET status = ?, ended_at = ?, duration_seconds = ?,
                      error = ?
               WHERE execution_id = ?""",
            (
                final_status,
                ended.isoformat(),
                max(0.0, (ended - started).total_seconds()),
                execution_error,
                identifier,
            ),
        )
    return identifier
