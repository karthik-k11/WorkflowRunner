from models.task import Task
from models.workflow import Workflow


SUPPORTED_TASK_TYPES = {"python", "file", "command"}


def validate_workflow(workflow: Workflow, tasks: list[Task]) -> list[str]:
    """Return validation errors found in a workflow definition."""
    errors: list[str] = []

    if not isinstance(workflow.name, str) or not workflow.name.strip():
        errors.append("Workflow name must not be empty.")

    if not isinstance(tasks, list):
        return errors + ["Workflow tasks must be provided as a list."]

    task_ids: set[str] = set()
    tasks_by_id: dict[str, Task] = {}

    # Validate individual task definitions.
    for index, task in enumerate(tasks):
        if not isinstance(task, Task):
            errors.append(f"Task at index {index} must be a Task instance.")
            continue

        if not isinstance(task.task_id, str) or not task.task_id.strip():
            errors.append(f"Task at index {index} must have a non-empty ID.")
        elif task.task_id in task_ids:
            errors.append(f"Duplicate task ID: '{task.task_id}'.")
        else:
            task_ids.add(task.task_id)
            tasks_by_id[task.task_id] = task

        if not isinstance(task.name, str) or not task.name.strip():
            errors.append(f"Task '{task.task_id}' must have a non-empty name.")

        if task.task_type not in SUPPORTED_TASK_TYPES:
            errors.append(
                f"Task '{task.task_id}' has unsupported task type "
                f"'{task.task_type}'."
            )

        if (
            isinstance(task.timeout_seconds, bool)
            or not isinstance(task.timeout_seconds, int)
            or task.timeout_seconds <= 0
        ):
            errors.append(
                f"Task '{task.task_id}' timeout must be a positive integer."
            )

        if not isinstance(task.config, dict):
            errors.append(f"Task '{task.task_id}' config must be a dictionary.")

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
