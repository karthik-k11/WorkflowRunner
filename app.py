"""Flask application for the local WorkflowRunner dashboard."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import Config
from database import db_session, init_db
from models.task import Task
from models.workflow import Workflow
from services.execution_engine import execute_workflow
from services.scheduler import LocalScheduler
from services.workflow_store import (
    definition_from_dict,
    definition_to_dict,
    get_workflow,
    list_workflows,
    now_iso,
    save_workflow,
)
from services.workflow_validator import resolve_execution_order, validate_workflow

logging.basicConfig(level=logging.INFO)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    app.config["START_SCHEDULER"] = True
    app.config["MAX_PARALLEL_TASKS"] = 2
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["FILE_ROOT"]).mkdir(parents=True, exist_ok=True)
    init_db(app.config["DATABASE"])
    if not test_config or "MAX_PARALLEL_TASKS" not in test_config:
        with db_session(app.config["DATABASE"]) as connection:
            setting = connection.execute(
                "SELECT value FROM settings WHERE key = 'max_parallel_tasks'"
            ).fetchone()
        if setting:
            try:
                configured_parallel = int(setting["value"])
            except ValueError:
                configured_parallel = 0
            if 1 <= configured_parallel <= 4:
                app.config["MAX_PARALLEL_TASKS"] = configured_parallel

    active_workflows: set[str] = set()
    cancellation_events: dict[str, threading.Event] = {}
    execution_lock = threading.Lock()
    executor = ThreadPoolExecutor(
        max_workers=max(1, int(app.config["MAX_PARALLEL_TASKS"])),
        thread_name_prefix="workflowrunner",
    )

    def launch(workflow_id: str) -> str:
        with execution_lock:
            if workflow_id in active_workflows:
                raise RuntimeError("This workflow already has an active execution.")
            with db_session(app.config["DATABASE"]) as connection:
                connection.execute("BEGIN IMMEDIATE")
                found = get_workflow(connection, workflow_id)
                if found is None:
                    raise ValueError("Workflow not found.")
                workflow, tasks = found
                active = connection.execute(
                    """SELECT 1 FROM executions WHERE workflow_id = ?
                       AND status IN ('queued', 'running') LIMIT 1""",
                    (workflow_id,),
                ).fetchone()
                if active:
                    raise RuntimeError("This workflow already has an active execution.")
            execution_id = str(uuid4())
            cancellation = threading.Event()
            active_workflows.add(workflow_id)
            cancellation_events[execution_id] = cancellation
            with db_session(app.config["DATABASE"]) as connection:
                connection.execute(
                    """INSERT INTO executions
                       (execution_id, workflow_id, status, started_at)
                       VALUES (?, ?, 'queued', ?)""",
                    (execution_id, workflow_id, now_iso()),
                )

            def execute() -> None:
                try:
                    execute_workflow(
                        app.config["DATABASE"],
                        workflow,
                        tasks,
                        Path(app.config["FILE_ROOT"]),
                        app.config["ALLOWED_COMMANDS"],
                        output_limit=int(app.config["OUTPUT_LIMIT"]),
                        max_retries=int(app.config["MAX_RETRIES"]),
                        max_parallel=int(app.config["MAX_PARALLEL_TASKS"]),
                        cancel_event=cancellation,
                        execution_id=execution_id,
                    )
                except Exception as exc:
                    logging.getLogger(__name__).exception(
                        "Execution %s failed before the engine could record its result.",
                        execution_id,
                    )
                    with db_session(app.config["DATABASE"]) as connection:
                        connection.execute(
                            """UPDATE executions
                               SET status = 'failed', ended_at = ?, error = ?
                               WHERE execution_id = ? AND status IN ('queued', 'running')""",
                            (now_iso(), str(exc)[: int(app.config["OUTPUT_LIMIT"])], execution_id),
                        )
                finally:
                    with execution_lock:
                        active_workflows.discard(workflow_id)
                        cancellation_events.pop(execution_id, None)

            executor.submit(execute)
            return execution_id

    app.extensions["workflowrunner_launch"] = launch
    app.extensions["workflowrunner_executor"] = executor

    if app.config["START_SCHEDULER"]:
        scheduler = LocalScheduler(app.config["DATABASE"], launch)
        scheduler.start()
        app.extensions["workflowrunner_scheduler"] = scheduler

    @app.before_request
    def protect_state_changes() -> None:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        expected = session.get("_csrf_token")
        supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, description="Invalid or missing CSRF token.")

    @app.context_processor
    def inject_template_values() -> dict[str, Any]:
        token = session.setdefault("_csrf_token", secrets.token_urlsafe(32))
        return {
            "csrf_token": token,
            "active_page": request.endpoint or "",
            "app_name": "WorkflowRunner",
        }

    @app.teardown_appcontext
    def close_context(_: BaseException | None) -> None:
        g.pop("unused", None)

    def workflow_or_404(workflow_id: str) -> tuple[Workflow, list[Task]]:
        with db_session(current_app.config["DATABASE"]) as connection:
            found = get_workflow(connection, workflow_id)
        if found is None:
            abort(404)
        return found

    def parse_form_definition() -> tuple[Workflow, list[Task]]:
        name = request.form.get("name", "")
        description = request.form.get("description", "")
        workflow = Workflow(name=name, description=description)
        try:
            raw_tasks = json.loads(request.form.get("tasks_json", "[]"))
        except json.JSONDecodeError as exc:
            raise ValueError("Task configuration could not be read.") from exc
        if not isinstance(raw_tasks, list) or len(raw_tasks) > 100:
            raise ValueError("A workflow can contain at most 100 tasks.")
        tasks = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                raise ValueError("Each task must be a structured object.")
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
        return workflow, tasks

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "ok"})

    @app.get("/")
    def dashboard() -> str:
        with db_session(current_app.config["DATABASE"]) as connection:
            metrics = {
                "workflows": connection.execute(
                    "SELECT COUNT(*) FROM workflows WHERE deleted_at IS NULL"
                ).fetchone()[0],
                "executions": connection.execute(
                    "SELECT COUNT(*) FROM executions"
                ).fetchone()[0],
                "successes": connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE status = 'success'"
                ).fetchone()[0],
                "failures": connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE status IN ('failed', 'interrupted')"
                ).fetchone()[0],
                "running": connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE status IN ('queued', 'running')"
                ).fetchone()[0],
            }
            recent = connection.execute(
                """SELECT e.*, w.name AS workflow_name
                   FROM executions e JOIN workflows w USING(workflow_id)
                   ORDER BY e.started_at DESC LIMIT 8"""
            ).fetchall()
            workflows = list_workflows(connection)[:5]
        return render_template(
            "dashboard.html",
            metrics=metrics,
            recent=recent,
            workflows=workflows,
        )

    @app.get("/workflows")
    def workflows() -> str:
        query = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "")
        with db_session(current_app.config["DATABASE"]) as connection:
            rows = list_workflows(connection)
        if query:
            rows = [row for row in rows if query.casefold() in row["name"].casefold()]
        if status_filter in {"success", "failed", "running"}:
            rows = [
                row
                for row in rows
                if row["last_status"] == status_filter
                or (status_filter == "running" and row["last_status"] == "queued")
            ]
        return render_template(
            "workflows.html", workflows=rows, query=query, status_filter=status_filter
        )

    @app.route("/workflows/new", methods=["GET"])
    @app.route("/workflows/<workflow_id>/edit", methods=["GET"])
    def workflow_form(workflow_id: str | None = None) -> str:
        workflow = Workflow(name="", description="")
        tasks: list[Task] = []
        if workflow_id:
            workflow, tasks = workflow_or_404(workflow_id)
        task_data = [
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
        ]
        return render_template(
            "workflow_form.html",
            workflow=workflow,
            tasks=tasks,
            task_data=task_data,
            is_edit=workflow_id is not None,
            task_types=sorted({"python", "file", "command"}),
        )

    @app.post("/workflows/save")
    def workflow_save() -> Any:
        workflow_id = request.form.get("workflow_id") or None
        try:
            workflow, tasks = parse_form_definition()
            with db_session(current_app.config["DATABASE"]) as connection:
                saved = save_workflow(connection, workflow, tasks, workflow_id)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("workflow_form", workflow_id=workflow_id) if workflow_id else url_for("workflow_form"))
        flash("Workflow saved.", "success")
        return redirect(url_for("workflow_detail", workflow_id=saved.workflow_id))

    @app.get("/workflows/<workflow_id>")
    def workflow_detail(workflow_id: str) -> str:
        workflow, tasks = workflow_or_404(workflow_id)
        errors = validate_workflow(workflow, tasks)
        try:
            order = resolve_execution_order(tasks)
        except ValueError:
            order = []
        with db_session(current_app.config["DATABASE"]) as connection:
            executions = connection.execute(
                """SELECT * FROM executions WHERE workflow_id = ?
                   ORDER BY started_at DESC LIMIT 10""",
                (workflow_id,),
            ).fetchall()
            running = connection.execute(
                """SELECT 1 FROM executions WHERE workflow_id = ?
                   AND status IN ('queued', 'running') LIMIT 1""",
                (workflow_id,),
            ).fetchone() is not None
        return render_template(
            "workflow_detail.html",
            workflow=workflow,
            tasks=tasks,
            validation_errors=errors,
            order=order,
            executions=executions,
            running=running,
        )

    @app.post("/workflows/<workflow_id>/run")
    def workflow_run(workflow_id: str) -> Any:
        try:
            execution_id = launch(workflow_id)
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("workflow_detail", workflow_id=workflow_id))
        flash("Execution started.", "success")
        return redirect(url_for("execution_detail", execution_id=execution_id))

    @app.post("/workflows/<workflow_id>/validate")
    def workflow_validate(workflow_id: str) -> Any:
        workflow, tasks = workflow_or_404(workflow_id)
        errors = validate_workflow(workflow, tasks)
        if errors:
            flash(" ".join(errors), "error")
        else:
            flash("Workflow definition is valid.", "success")
        return redirect(url_for("workflow_detail", workflow_id=workflow.workflow_id))

    @app.post("/workflows/<workflow_id>/delete")
    def workflow_delete(workflow_id: str) -> Any:
        with db_session(current_app.config["DATABASE"]) as connection:
            active = connection.execute(
                """SELECT 1 FROM executions WHERE workflow_id = ?
                   AND status IN ('queued', 'running') LIMIT 1""",
                (workflow_id,),
            ).fetchone()
            if active:
                flash("A running workflow cannot be deleted.", "error")
                return redirect(url_for("workflow_detail", workflow_id=workflow_id))
            connection.execute(
                "UPDATE workflows SET deleted_at = ?, updated_at = ? WHERE workflow_id = ?",
                (now_iso(), now_iso(), workflow_id),
            )
            connection.execute(
                "UPDATE schedules SET enabled = 0 WHERE workflow_id = ?",
                (workflow_id,),
            )
        flash("Workflow archived. Its execution history has been retained.", "success")
        return redirect(url_for("workflows"))

    def remap_references(value: Any, identifiers: dict[str, str]) -> Any:
        if isinstance(value, dict):
            remapped = {
                key: remap_references(item, identifiers) for key, item in value.items()
            }
            if remapped.get("from_task") in identifiers:
                remapped["from_task"] = identifiers[remapped["from_task"]]
            return remapped
        if isinstance(value, list):
            return [remap_references(item, identifiers) for item in value]
        return value

    @app.post("/workflows/<workflow_id>/duplicate")
    def workflow_duplicate(workflow_id: str) -> Any:
        workflow, tasks = workflow_or_404(workflow_id)
        id_map = {task.task_id: str(uuid4())[:8] for task in tasks}
        duplicate_tasks = [
            Task(
                task_id=id_map[task.task_id],
                name=task.name,
                task_type=task.task_type,
                config=remap_references(task.config, id_map),
                dependencies=[id_map[dep] for dep in task.dependencies],
                timeout_seconds=task.timeout_seconds,
                enabled=task.enabled,
            )
            for task in tasks
        ]
        duplicate = Workflow(
            name=f"{workflow.name} copy",
            description=workflow.description,
        )
        with db_session(current_app.config["DATABASE"]) as connection:
            saved = save_workflow(connection, duplicate, duplicate_tasks)
        flash("Workflow duplicated.", "success")
        return redirect(url_for("workflow_detail", workflow_id=saved.workflow_id))

    @app.get("/executions")
    def executions() -> str:
        status_filter = request.args.get("status", "")
        with db_session(current_app.config["DATABASE"]) as connection:
            query = """SELECT e.*, w.name AS workflow_name
                       FROM executions e JOIN workflows w USING(workflow_id)"""
            params: tuple[Any, ...] = ()
            if status_filter in {"success", "failed", "running", "cancelled", "interrupted"}:
                query += " WHERE e.status = ?"
                params = (status_filter,)
            query += " ORDER BY e.started_at DESC LIMIT 200"
            rows = connection.execute(query, params).fetchall()
        return render_template(
            "executions.html", executions=rows, status_filter=status_filter
        )

    @app.get("/executions/<execution_id>")
    def execution_detail(execution_id: str) -> str:
        with db_session(current_app.config["DATABASE"]) as connection:
            execution = connection.execute(
                """SELECT e.*, w.name AS workflow_name
                   FROM executions e JOIN workflows w USING(workflow_id)
                   WHERE execution_id = ?""",
                (execution_id,),
            ).fetchone()
            if execution is None:
                abort(404)
            tasks = connection.execute(
                """SELECT * FROM execution_tasks WHERE execution_id = ?
                   ORDER BY rowid""",
                (execution_id,),
            ).fetchall()
            other_executions = connection.execute(
                """SELECT execution_id, status, started_at FROM executions
                   WHERE workflow_id = ? AND execution_id != ?
                   ORDER BY started_at DESC LIMIT 50""",
                (execution["workflow_id"], execution_id),
            ).fetchall()
        return render_template(
            "execution_detail.html",
            execution=execution,
            tasks=tasks,
            other_executions=other_executions,
        )

    @app.get("/executions/compare")
    def execution_compare() -> Any:
        left_id = request.args.get("left", "")
        right_id = request.args.get("right", "")
        if not left_id or not right_id or left_id == right_id:
            abort(400, description="Choose two different executions to compare.")
        with db_session(current_app.config["DATABASE"]) as connection:
            runs = connection.execute(
                """SELECT e.*, w.name AS workflow_name FROM executions e
                   JOIN workflows w USING(workflow_id)
                   WHERE e.execution_id IN (?, ?)""",
                (left_id, right_id),
            ).fetchall()
            if len(runs) != 2 or runs[0]["workflow_id"] != runs[1]["workflow_id"]:
                abort(400, description="Executions must belong to the same workflow.")
            task_rows = connection.execute(
                """SELECT * FROM execution_tasks
                   WHERE execution_id IN (?, ?) ORDER BY task_id""",
                (left_id, right_id),
            ).fetchall()
        run_map = {run["execution_id"]: run for run in runs}
        if run_map[left_id]["started_at"] > run_map[right_id]["started_at"]:
            left_id, right_id = right_id, left_id
        task_map = {
            run_id: {row["task_id"]: row for row in task_rows if row["execution_id"] == run_id}
            for run_id in (left_id, right_id)
        }
        task_comparison = []
        for task_id in sorted(set(task_map[left_id]) | set(task_map[right_id])):
            before = task_map[left_id].get(task_id)
            after = task_map[right_id].get(task_id)
            task_comparison.append(
                {
                    "task_id": task_id,
                    "name": (after or before)["task_name"],
                    "left": before,
                    "right": after,
                    "duration_delta": (
                        (after["duration_seconds"] or 0) - (before["duration_seconds"] or 0)
                        if before and after
                        else None
                    ),
                    "output_changed": (
                        before["output_json"] != after["output_json"]
                        if before and after
                        else None
                    ),
                }
            )
        return render_template(
            "execution_compare.html",
            left=run_map[left_id],
            right=run_map[right_id],
            task_comparison=task_comparison,
        )

    @app.get("/executions/<execution_id>/report")
    def execution_report(execution_id: str) -> Any:
        with db_session(current_app.config["DATABASE"]) as connection:
            execution = connection.execute(
                """SELECT e.*, w.name AS workflow_name FROM executions e
                   JOIN workflows w USING(workflow_id) WHERE execution_id = ?""",
                (execution_id,),
            ).fetchone()
            if execution is None:
                abort(404)
            task_rows = connection.execute(
                """SELECT task_id, task_name, status, started_at, ended_at,
                          duration_seconds, output_json, error
                   FROM execution_tasks WHERE execution_id = ? ORDER BY rowid""",
                (execution_id,),
            ).fetchall()
        report = {
            "report_version": 1,
            "execution": dict(execution),
            "tasks": [
                {
                    **{
                        key: row[key]
                        for key in (
                            "task_id",
                            "task_name",
                            "status",
                            "started_at",
                            "ended_at",
                            "duration_seconds",
                            "error",
                        )
                    },
                    "output": json.loads(row["output_json"])
                    if row["output_json"]
                    else None,
                }
                for row in task_rows
            ],
        }
        response = make_response(
            json.dumps(report, indent=2, ensure_ascii=False)
        )
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="execution-{execution_id}.json"'
        )
        return response

    @app.post("/executions/<execution_id>/cancel")
    def execution_cancel(execution_id: str) -> Any:
        cancellation = cancellation_events.get(execution_id)
        if cancellation is None:
            flash("This execution is no longer active.", "error")
        else:
            cancellation.set()
            flash("Cancellation requested. Active tasks finish before cancellation is applied.", "success")
        return redirect(url_for("execution_detail", execution_id=execution_id))

    @app.get("/templates")
    def template_list() -> str:
        with db_session(current_app.config["DATABASE"]) as connection:
            templates = connection.execute(
                "SELECT * FROM workflow_templates ORDER BY name"
            ).fetchall()
        return render_template("templates.html", templates=templates)

    @app.post("/workflows/<workflow_id>/template")
    def template_create(workflow_id: str) -> Any:
        workflow, tasks = workflow_or_404(workflow_id)
        with db_session(current_app.config["DATABASE"]) as connection:
            connection.execute(
                """INSERT INTO workflow_templates
                   (template_id, name, description, definition_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    request.form.get("template_name", workflow.name).strip() or workflow.name,
                    workflow.description,
                    json.dumps(definition_to_dict(workflow, tasks)),
                    now_iso(),
                ),
            )
        flash("Reusable template created.", "success")
        return redirect(url_for("template_list"))

    @app.post("/templates/<template_id>/use")
    def template_use(template_id: str) -> Any:
        with db_session(current_app.config["DATABASE"]) as connection:
            row = connection.execute(
                "SELECT definition_json FROM workflow_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        if row is None:
            abort(404)
        workflow, tasks = definition_from_dict(json.loads(row["definition_json"]))
        workflow.name = f"{workflow.name} (from template)"
        with db_session(current_app.config["DATABASE"]) as connection:
            saved = save_workflow(connection, workflow, tasks)
        flash("Workflow created from template.", "success")
        return redirect(url_for("workflow_detail", workflow_id=saved.workflow_id))

    @app.post("/templates/<template_id>/delete")
    def template_delete(template_id: str) -> Any:
        with db_session(current_app.config["DATABASE"]) as connection:
            connection.execute(
                "DELETE FROM workflow_templates WHERE template_id = ?", (template_id,)
            )
        flash("Template deleted.", "success")
        return redirect(url_for("template_list"))

    @app.get("/schedules")
    def schedules() -> str:
        with db_session(current_app.config["DATABASE"]) as connection:
            rows = connection.execute(
                """SELECT s.*, w.name AS workflow_name
                   FROM schedules s JOIN workflows w USING(workflow_id)
                   WHERE w.deleted_at IS NULL ORDER BY s.created_at DESC"""
            ).fetchall()
            workflow_rows = connection.execute(
                "SELECT workflow_id, name FROM workflows WHERE deleted_at IS NULL ORDER BY name"
            ).fetchall()
        return render_template("schedules.html", schedules=rows, workflows=workflow_rows)

    @app.post("/schedules")
    def schedule_create() -> Any:
        workflow_id = request.form.get("workflow_id", "")
        try:
            interval = int(request.form.get("interval_minutes", ""))
        except ValueError:
            interval = 0
        if not 1 <= interval <= 43_200:
            flash("Schedule interval must be between 1 minute and 30 days.", "error")
            return redirect(url_for("schedules"))
        with db_session(current_app.config["DATABASE"]) as connection:
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE workflow_id = ? AND deleted_at IS NULL",
                (workflow_id,),
            ).fetchone()
            if not exists:
                flash("Select an existing workflow.", "error")
                return redirect(url_for("schedules"))
            next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)
            connection.execute(
                """INSERT INTO schedules
                   (schedule_id, workflow_id, interval_minutes, next_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(uuid4()), workflow_id, interval, next_run.isoformat(), now_iso()),
            )
        flash("Schedule created.", "success")
        return redirect(url_for("schedules"))

    @app.post("/schedules/<schedule_id>/toggle")
    def schedule_toggle(schedule_id: str) -> Any:
        with db_session(current_app.config["DATABASE"]) as connection:
            connection.execute(
                "UPDATE schedules SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE schedule_id = ?",
                (schedule_id,),
            )
        flash("Schedule updated.", "success")
        return redirect(url_for("schedules"))

    @app.post("/schedules/<schedule_id>/delete")
    def schedule_delete(schedule_id: str) -> Any:
        with db_session(current_app.config["DATABASE"]) as connection:
            connection.execute("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
        flash("Schedule deleted.", "success")
        return redirect(url_for("schedules"))

    @app.get("/workflows/<workflow_id>/export")
    def workflow_export(workflow_id: str) -> Any:
        workflow, tasks = workflow_or_404(workflow_id)
        response = make_response(
            json.dumps(definition_to_dict(workflow, tasks), indent=2, ensure_ascii=False)
        )
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="workflow-{workflow.workflow_id}.json"'
        )
        return response

    @app.post("/workflows/import")
    def workflow_import() -> Any:
        uploaded = request.files.get("workflow_file")
        if uploaded is None or uploaded.filename == "":
            flash("Choose a workflow JSON file to import.", "error")
            return redirect(url_for("workflows"))
        try:
            raw = uploaded.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("Import files must be no larger than 1 MB.")
            workflow, tasks = definition_from_dict(json.loads(raw.decode("utf-8")))
            with db_session(current_app.config["DATABASE"]) as connection:
                saved = save_workflow(connection, workflow, tasks)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            flash(f"Workflow import rejected: {exc}", "error")
            return redirect(url_for("workflows"))
        flash("Workflow imported and validated.", "success")
        return redirect(url_for("workflow_detail", workflow_id=saved.workflow_id))

    @app.route("/settings", methods=["GET", "POST"])
    def settings() -> Any:
        if request.method == "POST":
            try:
                concurrency = int(request.form.get("max_parallel_tasks", ""))
            except ValueError:
                concurrency = 0
            if not 1 <= concurrency <= 4:
                flash("Concurrency must be between 1 and 4.", "error")
            else:
                current_app.config["MAX_PARALLEL_TASKS"] = concurrency
                with db_session(current_app.config["DATABASE"]) as connection:
                    connection.execute(
                        """INSERT INTO settings (key, value) VALUES ('max_parallel_tasks', ?)
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                        (str(concurrency),),
                    )
                flash("Runtime setting updated. New executions use this limit.", "success")
            return redirect(url_for("settings"))
        return render_template(
            "settings.html",
            file_root=current_app.config["FILE_ROOT"],
            commands=sorted(current_app.config["ALLOWED_COMMANDS"]),
            max_parallel=current_app.config["MAX_PARALLEL_TASKS"],
        )

    @app.template_filter("datetime")
    def format_datetime(value: Any) -> str:
        if not value:
            return "—"
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
            return parsed.astimezone().strftime("%b %d, %Y · %H:%M")
        except (TypeError, ValueError):
            return str(value)

    @app.errorhandler(400)
    @app.errorhandler(404)
    def handle_http_error(error: Any) -> tuple[str, int]:
        return (
            render_template(
                "error.html",
                code=error.code,
                message=error.description if error.code == 400 else "The requested page was not found.",
            ),
            error.code,
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)