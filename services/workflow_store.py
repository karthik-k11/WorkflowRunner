"""Persistence helpers for workflow definitions."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from models.task import Task
from models.workflow import Workflow
from services.workflow_validator import validate_workflow


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        name=row["name"],
        task_type=row["task_type"],
        config=json.loads(row["config_json"]),
        dependencies=json.loads(row["dependencies_json"]),
        timeout_seconds=row["timeout_seconds"],
        enabled=bool(row["enabled"]),
    )


def get_workflow(
    connection: sqlite3.Connection, workflow_id: str
) -> tuple[Workflow, list[Task]] | None:
    row = connection.execute(
        "SELECT * FROM workflows WHERE workflow_id = ? AND deleted_at IS NULL",
        (workflow_id,),
    ).fetchone()
    if row is None:
        return None
    workflow = Workflow(
        name=row["name"],
        description=row["description"],
        workflow_id=row["workflow_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
    task_rows = connection.execute(
        "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY position, task_id",
        (workflow_id,),
    ).fetchall()
    return workflow, [_task_from_row(task_row) for task_row in task_rows]


def list_workflows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT w.*, COUNT(DISTINCT t.task_id) AS task_count,
                  (SELECT COUNT(*) FROM executions e
                   WHERE e.workflow_id = w.workflow_id) AS execution_count,
                  (SELECT status FROM executions e
                   WHERE e.workflow_id = w.workflow_id
                   ORDER BY started_at DESC LIMIT 1) AS last_status
           FROM workflows w LEFT JOIN tasks t ON t.workflow_id = w.workflow_id
           WHERE w.deleted_at IS NULL
           GROUP BY w.workflow_id ORDER BY w.updated_at DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def save_workflow(
    connection: sqlite3.Connection,
    workflow: Workflow,
    tasks: list[Task],
    workflow_id: str | None = None,
) -> Workflow:
    errors = validate_workflow(workflow, tasks)
    if errors:
        raise ValueError("; ".join(errors))
    timestamp = now_iso()
    identifier = workflow_id or workflow.workflow_id or str(uuid4())
    exists = connection.execute(
        "SELECT 1 FROM workflows WHERE workflow_id = ?", (identifier,)
    ).fetchone()
    if exists:
        connection.execute(
            "UPDATE workflows SET name = ?, description = ?, updated_at = ?, deleted_at = NULL "
            "WHERE workflow_id = ?",
            (workflow.name.strip(), workflow.description.strip(), timestamp, identifier),
        )
        connection.execute("DELETE FROM tasks WHERE workflow_id = ?", (identifier,))
    else:
        connection.execute(
            """INSERT INTO workflows
               (workflow_id, name, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (identifier, workflow.name.strip(), workflow.description.strip(), timestamp, timestamp),
        )
    for position, task in enumerate(tasks):
        connection.execute(
            """INSERT INTO tasks
               (task_id, workflow_id, name, task_type, config_json,
                dependencies_json, timeout_seconds, enabled, position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.task_id,
                identifier,
                task.name.strip(),
                task.task_type,
                json.dumps(task.config, ensure_ascii=False),
                json.dumps(task.dependencies),
                task.timeout_seconds,
                int(task.enabled),
                position,
            ),
        )
    return Workflow(
        name=workflow.name.strip(),
        description=workflow.description.strip(),
        workflow_id=identifier,
        created_at=workflow.created_at,
        updated_at=datetime.fromisoformat(timestamp),
    )


def definition_to_dict(workflow: Workflow, tasks: list[Task]) -> dict[str, Any]:
    return {
        "version": 1,
        "workflow": {"name": workflow.name, "description": workflow.description},
        "tasks": [
            {
                "task_id": task.task_id,
                "name": task.name,
                "task_type": task.task_type,
                "config": task.config,
                "dependencies": task.dependencies,
                "timeout_seconds": task.timeout_seconds,
                "enabled": task.enabled,
            }
            for task in tasks
        ],
    }


def definition_from_dict(data: Any) -> tuple[Workflow, list[Task]]:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Import must be a version 1 WorkflowRunner definition.")
    workflow_data = data.get("workflow")
    task_data = data.get("tasks")
    if not isinstance(workflow_data, dict) or not isinstance(task_data, list):
        raise ValueError("Import must include a workflow object and tasks list.")
    if len(task_data) > 100:
        raise ValueError("Workflow imports can contain at most 100 tasks.")
    workflow = Workflow(
        name=workflow_data.get("name", ""),
        description=workflow_data.get("description", ""),
    )
    tasks: list[Task] = []
    for item in task_data:
        if not isinstance(item, dict):
            raise ValueError("Every imported task must be an object.")
        tasks.append(
            Task(
                task_id=item.get("task_id", ""),
                name=item.get("name", ""),
                task_type=item.get("task_type", ""),
                config=item.get("config", {}),
                dependencies=item.get("dependencies", []),
                timeout_seconds=item.get("timeout_seconds", 30),
                enabled=item.get("enabled", True),
            )
        )
    errors = validate_workflow(workflow, tasks)
    if errors:
        raise ValueError("; ".join(errors))
    return workflow, tasks
