import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from database import db_session, init_db
from models.task import Task
from models.workflow import Workflow
from services.execution_engine import execute_workflow
from services.task_runner import TaskExecutionError, _command_operation, execute_task
from services.scheduler import run_due_schedules
from services.workflow_store import definition_from_dict, save_workflow


@pytest.fixture
def runtime(tmp_path):
    database = tmp_path / "workflow.sqlite3"
    file_root = tmp_path / "files"
    init_db(database)
    return database, file_root


def persist_workflow(database, workflow, tasks):
    with db_session(database) as connection:
        save_workflow(connection, workflow, tasks)


def read_execution(database, execution_id):
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        execution = connection.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        tasks = connection.execute(
            "SELECT * FROM execution_tasks WHERE execution_id = ? ORDER BY task_id",
            (execution_id,),
        ).fetchall()
    return execution, tasks


def run(database, file_root, workflow, tasks, **options):
    return execute_workflow(
        database,
        workflow,
        tasks,
        file_root,
        {"python-version": [sys.executable, "--version"], "git-version": ["git", "--version"]},
        **options,
    )


def test_success_mapping_and_persisted_history(runtime):
    database, file_root = runtime
    workflow = Workflow(name="Mapped workflow")
    tasks = [
        Task("first", "Produce value", "python", {"operation": "echo", "value": "hello"}),
        Task(
            "second",
            "Use value",
            "python",
            {"operation": "uppercase", "inputs": {"value": {"from_task": "first", "path": "value"}}},
            ["first"],
        ),
    ]
    persist_workflow(database, workflow, tasks)

    execution_id = run(database, file_root, workflow, tasks, max_parallel=2)
    execution, rows = read_execution(database, execution_id)

    assert execution["status"] == "success"
    assert len(rows) == 2
    assert all(row["status"] == "success" for row in rows)
    assert json.loads(rows[1]["output_json"])["value"] == "HELLO"
    assert execution["ended_at"] is not None
    assert execution["duration_seconds"] >= 0


def test_failed_task_blocks_dependents_but_unrelated_branch_runs(runtime):
    database, file_root = runtime
    workflow = Workflow(name="Failure policy")
    tasks = [
        Task("bad", "Missing file", "file", {"action": "read_text", "path": "missing.txt"}),
        Task("dependent", "Dependent", "python", {"operation": "echo"}, ["bad"]),
        Task("independent", "Independent", "python", {"operation": "echo", "value": "ok"}),
    ]
    persist_workflow(database, workflow, tasks)

    execution_id = run(database, file_root, workflow, tasks)
    execution, rows = read_execution(database, execution_id)
    statuses = {row["task_id"]: row["status"] for row in rows}

    assert execution["status"] == "failed"
    assert statuses == {"bad": "failed", "dependent": "blocked", "independent": "success"}


def test_file_operations_are_confined_and_command_timeout_is_enforced(runtime):
    _, file_root = runtime
    file_root.mkdir()
    (file_root / "notes.txt").write_text("safe", encoding="utf-8")
    outside = file_root.parent / "secret.txt"
    outside.write_text("private", encoding="utf-8")

    with pytest.raises(TaskExecutionError, match="outside the configured file root"):
        execute_task(
            "file",
            {"action": "read_text", "path": "..\\secret.txt"},
            file_root,
            {},
            2,
            100,
        )

    with pytest.raises(TaskExecutionError, match="timeout"):
        _command_operation(
            {"command": "wait"},
            {"wait": [sys.executable, "-c", "import time; time.sleep(2)"]},
            1,
            50,
        )


def test_nonzero_command_exit_is_reported():
    with pytest.raises(TaskExecutionError, match="exited with code 7"):
        _command_operation(
            {"command": "fail"},
            {"fail": [sys.executable, "-c", "raise SystemExit(7)"]},
            2,
            100,
        )


def test_conditions_retries_and_independent_parallelism(runtime, monkeypatch):
    database, file_root = runtime
    workflow = Workflow(name="Conditional workflow")
    tasks = [
        Task("source", "Return empty", "python", {"operation": "echo", "value": ""}),
        Task(
            "conditional",
            "Run only when source is truthy",
            "python",
            {
                "operation": "echo",
                "condition": {
                    "operator": "truthy",
                    "left": {"from_task": "source", "path": "value"},
                },
            },
            ["source"],
        ),
        Task("blocked", "Needs conditional", "python", {"operation": "echo"}, ["conditional"]),
        Task(
            "retry",
            "Retry once",
            "python",
            {"operation": "echo", "retry": {"attempts": 2}},
        ),
    ]
    persist_workflow(database, workflow, tasks)
    calls = 0
    from services import execution_engine

    original = execution_engine.execute_task

    def flaky(*args, **kwargs):
        nonlocal calls
        if args[0] == "python" and args[1].get("retry"):
            calls += 1
            if calls == 1:
                raise TaskExecutionError("transient test failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(execution_engine, "execute_task", flaky)
    execution_id = run(database, file_root, workflow, tasks, max_parallel=2)
    execution, rows = read_execution(database, execution_id)
    statuses = {row["task_id"]: row["status"] for row in rows}

    assert calls == 2
    assert execution["status"] == "failed"
    assert statuses == {
        "blocked": "blocked",
        "conditional": "skipped",
        "retry": "success",
        "source": "success",
    }


def test_concurrency_is_bounded_and_restart_marks_active_work_interrupted(runtime, monkeypatch):
    database, file_root = runtime
    workflow = Workflow(name="Parallel")
    tasks = [
        Task(f"task-{number}", f"Task {number}", "python", {"operation": "echo"})
        for number in range(4)
    ]
    persist_workflow(database, workflow, tasks)
    lock = threading.Lock()
    active = 0
    peak = 0
    from services import execution_engine

    original = execution_engine._run_one

    def measured(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(execution_engine, "_run_one", measured)
    execution_id = run(database, file_root, workflow, tasks, max_parallel=2)
    assert read_execution(database, execution_id)[0]["status"] == "success"
    assert peak == 2

    with db_session(database) as connection:
        connection.execute(
            """INSERT INTO executions
               (execution_id, workflow_id, status, started_at)
               VALUES ('stale', ?, 'running', '2026-01-01T00:00:00+00:00')""",
            (workflow.workflow_id,),
        )
        connection.execute(
            """INSERT INTO execution_tasks
               (execution_id, task_id, task_name, status)
               VALUES ('stale', 'task-0', 'Task 0', 'running')"""
        )
    init_db(database)
    execution, task_rows = read_execution(database, "stale")
    assert execution["status"] == "interrupted"
    assert task_rows[0]["status"] == "interrupted"


def test_scheduler_claims_due_schedule_once(runtime):
    database, _ = runtime
    workflow = Workflow(name="Scheduled")
    tasks = [Task("task", "Task", "python", {"operation": "echo"})]
    persist_workflow(database, workflow, tasks)
    with db_session(database) as connection:
        connection.execute(
            """INSERT INTO schedules
               (schedule_id, workflow_id, interval_minutes, next_run_at, created_at)
               VALUES ('schedule', ?, 10, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')""",
            (workflow.workflow_id,),
        )
    launches = []

    def launch(workflow_id):
        launches.append(workflow_id)
        return "scheduled-execution"

    assert run_due_schedules(database, launch) == 1
    assert run_due_schedules(database, launch) == 0
    assert launches == [workflow.workflow_id]
    with db_session(database) as connection:
        schedule = connection.execute(
            "SELECT next_run_at, last_execution_id FROM schedules WHERE schedule_id = 'schedule'"
        ).fetchone()
    assert schedule["last_execution_id"] == "scheduled-execution"
    assert schedule["next_run_at"] > "2020-01-01"


def test_file_copy_and_output_limits(runtime):
    _, file_root = runtime
    file_root.mkdir()
    (file_root / "source.txt").write_text("x" * 200, encoding="utf-8")
    commands = {
        "output": [
            sys.executable,
            "-c",
            "print('x' * 200)",
        ]
    }

    read_result = execute_task(
        "file", {"action": "read_text", "path": "source.txt"}, file_root, {}, 2, 40
    )
    assert len(read_result["text"]) == 40
    assert read_result["truncated"] is True

    copy_result = execute_task(
        "file",
        {"action": "copy", "source": "source.txt", "destination": "copies/result.txt"},
        file_root,
        {},
        2,
        100,
    )
    assert copy_result["copied"] is True
    assert (file_root / "copies" / "result.txt").read_text(encoding="utf-8") == "x" * 200
    with pytest.raises(TaskExecutionError, match="already exists"):
        execute_task(
            "file",
            {"action": "copy", "source": "source.txt", "destination": "copies/result.txt"},
            file_root,
            {},
            2,
            100,
        )

    command_result = _command_operation({"command": "output"}, commands, 2, 20)
    assert len(command_result["output"]) == 20
    assert command_result["truncated"] is True
    with pytest.raises(TaskExecutionError, match="finite numeric range"):
        execute_task(
            "python",
            {"operation": "add", "values": [1e308, 1e308]},
            file_root,
            {},
            2,
            100,
        )


def test_included_example_runs_within_its_file_root(runtime):
    database, file_root = runtime
    example_path = Path(__file__).resolve().parents[1] / "examples" / "getting-started.json"
    workflow, tasks = definition_from_dict(json.loads(example_path.read_text(encoding="utf-8")))
    persist_workflow(database, workflow, tasks)
    file_root.mkdir()
    sample = Path(__file__).resolve().parents[1] / "examples" / "sample.txt"
    (file_root / "sample.txt").write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")

    execution_id = run(database, file_root, workflow, tasks, max_parallel=2)
    execution, rows = read_execution(database, execution_id)
    statuses = {row["task_id"]: row["status"] for row in rows}

    assert execution["status"] == "success"
    assert all(status == "success" for status in statuses.values())


def test_cancellation_marks_tasks_cancelled_before_they_start(runtime):
    database, file_root = runtime
    workflow = Workflow(name="Cancelled")
    tasks = [Task("task", "Task", "python", {"operation": "echo"})]
    persist_workflow(database, workflow, tasks)
    cancellation = threading.Event()
    cancellation.set()

    execution_id = run(
        database, file_root, workflow, tasks, cancel_event=cancellation
    )
    execution, rows = read_execution(database, execution_id)

    assert execution["status"] == "cancelled"
    assert rows[0]["status"] == "cancelled"
