from models.task import Task
from models.workflow import Workflow
from services.workflow_validator import resolve_execution_order, validate_workflow


def test_dependency_order_is_stable_and_checks_all_graph_errors():
    tasks = [
        Task("finish", "Finish", "python", {"operation": "echo"}, ["middle"]),
        Task("middle", "Middle", "python", {"operation": "echo"}, ["start"]),
        Task("start", "Start", "python", {"operation": "echo"}),
        Task("other", "Other", "python", {"operation": "echo"}),
    ]

    ordered = resolve_execution_order(tasks)

    assert [task.task_id for task in ordered] == ["other", "start", "middle", "finish"]


def test_validation_rejects_duplicate_missing_self_and_cycle():
    workflow = Workflow(name="Invalid")
    tasks = [
        Task("one", "One", "python", {}, ["one", "missing", "two"]),
        Task("one", "Duplicate", "python", {}),
        Task("two", "Two", "python", {}, ["one"]),
    ]

    errors = validate_workflow(workflow, tasks)

    assert any("Duplicate task ID" in error for error in errors)
    assert any("cannot depend on itself" in error for error in errors)
    assert any("nonexistent task 'missing'" in error for error in errors)
    assert any("Circular dependency" in error for error in errors)


def test_validation_rejects_unsafe_file_and_unallowlisted_command():
    workflow = Workflow(name="Security")
    tasks = [
        Task("file", "Escape", "file", {"action": "read_text", "path": "..\\secret.txt"}),
        Task("command", "Shell", "command", {"command": "powershell"}),
    ]

    errors = validate_workflow(workflow, tasks)

    assert any("unsafe relative file path" in error for error in errors)
    assert any("not in the application allowlist" in error for error in errors)


def test_validation_rejects_non_finite_task_values():
    workflow = Workflow(name="Non-finite")
    task = Task("numeric", "Non-finite value", "python", {"value": float("nan")})

    errors = validate_workflow(workflow, [task])

    assert any("JSON-compatible values" in error for error in errors)
