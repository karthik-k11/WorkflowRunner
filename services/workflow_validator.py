import math
from pathlib import PureWindowsPath

from models.task import Task
from models.workflow import Workflow


SUPPORTED_TASK_TYPES = {"python", "file", "command"}
PYTHON_OPERATIONS = {"echo", "add", "uppercase", "count"}
FILE_ACTIONS = {"exists", "read_text", "copy"}
COMMAND_KEYS = {"python-version", "git-version"}


def _json_compatible(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_compatible(item)
            for key, item in value.items()
        )
    return False


def _validate_references(
    value: object, task_id: str, dependencies: set[str], errors: list[str]
) -> None:
    if isinstance(value, dict):
        if "from_task" in value:
            reference = value.get("from_task")
            if not isinstance(reference, str) or reference not in dependencies:
                errors.append(
                    f"Task '{task_id}' output mappings must reference a direct dependency."
                )
        for item in value.values():
            _validate_references(item, task_id, dependencies, errors)
    elif isinstance(value, list):
        for item in value:
            _validate_references(item, task_id, dependencies, errors)


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    windows_path = PureWindowsPath(value)
    normalized_parts = value.replace("\\", "/").split("/")
    return (
        not windows_path.is_absolute()
        and not windows_path.drive
        and not value.startswith(("/", "\\"))
        and ".." not in normalized_parts
        and "\x00" not in value
    )


def validate_workflow(workflow: Workflow, tasks: list[Task]) -> list[str]:
    """Return validation errors found in a workflow definition."""
    errors: list[str] = []

    if not isinstance(workflow.name, str) or not workflow.name.strip():
        errors.append("Workflow name must not be empty.")
    elif len(workflow.name) > 120:
        errors.append("Workflow name must be 120 characters or fewer.")
    if not isinstance(workflow.description, str) or len(workflow.description) > 1000:
        errors.append("Workflow description must be 1000 characters or fewer.")

    if not isinstance(tasks, list):
        return errors + ["Workflow tasks must be provided as a list."]
    if not tasks:
        errors.append("Workflow must contain at least one task.")

    task_ids: set[str] = set()
    tasks_by_id: dict[str, Task] = {}

    # Validate individual task definitions.
    for index, task in enumerate(tasks):
        if not isinstance(task, Task):
            errors.append(f"Task at index {index} must be a Task instance.")
            continue

        if not isinstance(task.task_id, str) or not task.task_id.strip():
            errors.append(f"Task at index {index} must have a non-empty ID.")
        elif len(task.task_id) > 80:
            errors.append(f"Task '{task.task_id[:20]}' ID must be 80 characters or fewer.")
        elif task.task_id in task_ids:
            errors.append(f"Duplicate task ID: '{task.task_id}'.")
        else:
            task_ids.add(task.task_id)
            tasks_by_id[task.task_id] = task

        if not isinstance(task.name, str) or not task.name.strip():
            errors.append(f"Task '{task.task_id}' must have a non-empty name.")
        elif len(task.name) > 120:
            errors.append(f"Task '{task.task_id}' name must be 120 characters or fewer.")

        if not isinstance(task.task_type, str) or task.task_type not in SUPPORTED_TASK_TYPES:
            errors.append(
                f"Task '{task.task_id}' has unsupported task type "
                f"'{task.task_type}'."
            )

        if (
            isinstance(task.timeout_seconds, bool)
            or not isinstance(task.timeout_seconds, int)
            or task.timeout_seconds <= 0
            or task.timeout_seconds > 300
        ):
            errors.append(
                f"Task '{task.task_id}' timeout must be an integer from 1 to 300."
            )

        if not isinstance(task.config, dict):
            errors.append(f"Task '{task.task_id}' config must be a dictionary.")
        elif isinstance(task.task_type, str):
            if not _json_compatible(task.config):
                errors.append(
                    f"Task '{task.task_id}' config must contain JSON-compatible values."
                )
            operation = task.config.get("operation", "echo")
            if task.task_type == "python" and (
                not isinstance(operation, str) or operation not in PYTHON_OPERATIONS
            ):
                errors.append(
                    f"Task '{task.task_id}' uses an unsupported Python operation."
                )
            if task.task_type == "file":
                action = task.config.get("action")
                if not isinstance(action, str) or action not in FILE_ACTIONS:
                    errors.append(f"Task '{task.task_id}' uses an unsupported file action.")
                path_keys = (
                    ("source", "destination")
                    if action == "copy"
                    else ("path",)
                )
                for key in path_keys:
                    if not _safe_relative_path(task.config.get(key)):
                        errors.append(
                            f"Task '{task.task_id}' has an unsafe relative file path."
                        )
            command = task.config.get("command")
            if task.task_type == "command" and (
                not isinstance(command, str) or command not in COMMAND_KEYS
            ):
                errors.append(
                    f"Task '{task.task_id}' command is not in the application allowlist."
                )
            retry = task.config.get("retry", {})
            if not isinstance(retry, dict):
                errors.append(f"Task '{task.task_id}' retry policy must be an object.")
            elif (
                isinstance(retry.get("attempts", 1), bool)
                or not isinstance(retry.get("attempts", 1), int)
                or not 1 <= retry.get("attempts", 1) <= 5
            ):
                errors.append(
                    f"Task '{task.task_id}' retry attempts must be between 1 and 5."
                )
            elif (
                isinstance(retry.get("delay_seconds", 0), bool)
                or not isinstance(retry.get("delay_seconds", 0), (int, float))
                or not 0 <= retry.get("delay_seconds", 0) <= 5
            ):
                errors.append(
                    f"Task '{task.task_id}' retry delay must be between 0 and 5 seconds."
                )
            inputs = task.config.get("inputs", {})
            if not isinstance(inputs, dict):
                errors.append(f"Task '{task.task_id}' inputs must be an object.")
            if not isinstance(task.enabled, bool):
                errors.append(f"Task '{task.task_id}' enabled must be a boolean.")
            condition = task.config.get("condition")
            if condition is not None:
                operator = condition.get("operator", "truthy") if isinstance(condition, dict) else None
                if not isinstance(operator, str) or operator not in {
                    "truthy", "falsy", "equals", "not_equals"
                }:
                    errors.append(f"Task '{task.task_id}' has an invalid condition.")
                elif not isinstance(condition, dict) or "left" not in condition:
                    errors.append(f"Task '{task.task_id}' condition requires a left value.")
            dependencies = {
                dep for dep in task.dependencies if isinstance(dep, str)
            } if isinstance(task.dependencies, list) else set()
            _validate_references(task.config, task.task_id, dependencies, errors)

        if not isinstance(task.dependencies, list):
            errors.append(
                f"Task '{task.task_id}' dependencies must be a list."
            )
        else:
            seen_dependencies: set[str] = set()

            for dependency in task.dependencies:
                if not isinstance(dependency, str) or not dependency.strip():
                    errors.append(
                        f"Task '{task.task_id}' has an invalid dependency ID."
                    )
                    continue

                if dependency in seen_dependencies:
                    errors.append(
                        f"Task '{task.task_id}' has duplicate dependency "
                        f"'{dependency}'."
                    )
                seen_dependencies.add(dependency)
                if dependency == task.task_id:
                    errors.append(
                        f"Task '{task.task_id}' cannot depend on itself."
                    )

    # Validate dependency references after collecting task IDs.
    for task in tasks:
        if not isinstance(task, Task):
            continue

        if not isinstance(task.dependencies, list):
            continue

        for dependency in task.dependencies:
            if isinstance(dependency, str) and dependency not in task_ids:
                errors.append(
                    f"Task '{task.task_id}' depends on nonexistent task "
                    f"'{dependency}'."
                )

    # Detect cycles using depth-first search.
    # Only follow dependencies that refer to known task IDs.
    graph = {
        task_id: [
            dependency
            for dependency in task.dependencies
            if isinstance(dependency, str) and dependency in tasks_by_id
        ]
        for task_id, task in tasks_by_id.items()
        if isinstance(task.dependencies, list)
    }

    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_found = False

    def visit(task_id: str) -> None:
        nonlocal cycle_found

        if task_id in visiting:
            cycle_found = True
            return

        if task_id in visited:
            return

        visiting.add(task_id)

        for dependency in graph.get(task_id, []):
            visit(dependency)

        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        if task_id not in visited:
            visit(task_id)

    if cycle_found:
        errors.append("Circular dependency detected in workflow.")

    return errors


def resolve_execution_order(tasks: list[Task]) -> list[Task]:
    """Return a deterministic topological order, rejecting invalid graphs."""
    workflow = Workflow(name="validation")
    errors = validate_workflow(workflow, tasks)
    if errors:
        raise ValueError("; ".join(errors))

    by_id = {task.task_id: task for task in tasks}
    remaining = set(by_id)
    resolved: set[str] = set()
    ordered: list[Task] = []
    while remaining:
        ready = sorted(
            task_id
            for task_id in remaining
            if set(by_id[task_id].dependencies).issubset(resolved)
        )
        if not ready:
            raise ValueError("Circular dependency detected in workflow.")
        for task_id in ready:
            ordered.append(by_id[task_id])
            remaining.remove(task_id)
            resolved.add(task_id)
    return ordered
