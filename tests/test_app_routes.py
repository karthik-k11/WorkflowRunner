import json
import io
import sqlite3
import time

import pytest

from app import create_app


@pytest.fixture
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "START_SCHEDULER": False,
            "SECRET_KEY": "test-key-not-for-production",
            "DATABASE": str(tmp_path / "app.sqlite3"),
            "FILE_ROOT": str(tmp_path / "files"),
        }
    )
    yield application
    application.extensions["workflowrunner_executor"].shutdown(wait=True)


@pytest.fixture
def client(app):
    return app.test_client()


def token(client):
    client.get("/")
    with client.session_transaction() as session:
        return session["_csrf_token"]


def save_workflow(client, name="Route workflow"):
    csrf = token(client)
    response = client.post(
        "/workflows/save",
        data={
            "_csrf_token": csrf,
            "name": name,
            "description": "A test workflow",
            "tasks_json": json.dumps(
                [
                    {
                        "task_id": "hello",
                        "name": "Say hello",
                        "task_type": "python",
                        "config": {"operation": "echo", "value": "hello"},
                        "dependencies": [],
                        "timeout_seconds": 5,
                        "enabled": True,
                    }
                ]
            ),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with client.application.app_context():
        with sqlite3.connect(client.application.config["DATABASE"]) as connection:
            row = connection.execute(
                "SELECT workflow_id FROM workflows WHERE name = ?", (name,)
            ).fetchone()
    return row[0]


def test_workflow_crud_run_history_and_csrf_protection(client, app):
    assert client.post("/workflows/save", data={"name": "missing token"}).status_code == 400
    assert client.get("/workflows/new").status_code == 200
    assert b"Create workflow" in client.get("/workflows/new").data
    workflow_id = save_workflow(client)
    assert client.get(f"/workflows/{workflow_id}").status_code == 200

    response = client.post(
        f"/workflows/{workflow_id}/run",
        data={"_csrf_token": token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 302
    execution_id = response.headers["Location"].rsplit("/", 1)[-1]
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        status = connection.execute(
            "SELECT status FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()[0]
    assert status in {"queued", "running", "success"}

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with sqlite3.connect(app.config["DATABASE"]) as connection:
            status = connection.execute(
                "SELECT status FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()[0]
        if status == "success":
            break
        time.sleep(0.02)
    assert status == "success"
    assert client.get(f"/executions/{execution_id}").status_code == 200
    dashboard = client.get("/")
    assert b"Successful" in dashboard.data
    assert b"Recent executions" in dashboard.data
    report = client.get(f"/executions/{execution_id}/report")
    assert report.status_code == 200
    assert json.loads(report.data)["tasks"][0]["output"]["value"] == "hello"

    second = client.post(
        f"/workflows/{workflow_id}/run",
        data={"_csrf_token": token(client)},
        follow_redirects=False,
    )
    second_id = second.headers["Location"].rsplit("/", 1)[-1]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with sqlite3.connect(app.config["DATABASE"]) as connection:
            status = connection.execute(
                "SELECT status FROM executions WHERE execution_id = ?", (second_id,)
            ).fetchone()[0]
        if status == "success":
            break
        time.sleep(0.02)
    comparison = client.get(
        f"/executions/compare?left={execution_id}&right={second_id}"
    )
    assert comparison.status_code == 200
    assert b"Compare runs" in comparison.data


def test_import_export_templates_schedules_and_archive(client, app):
    workflow_id = save_workflow(client, "Reusable")
    exported = client.get(f"/workflows/{workflow_id}/export")
    definition = json.loads(exported.data)
    assert exported.status_code == 200
    assert definition["workflow"]["name"] == "Reusable"

    response = client.post(
        f"/workflows/{workflow_id}/template",
        data={"_csrf_token": token(client), "template_name": "Shared template"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Shared template" in response.data

    with sqlite3.connect(app.config["DATABASE"]) as connection:
        template_id = connection.execute(
            "SELECT template_id FROM workflow_templates"
        ).fetchone()[0]
    created = client.post(
        f"/templates/{template_id}/use",
        data={"_csrf_token": token(client)},
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert b"Reusable (from template)" in created.data

    schedule = client.post(
        "/schedules",
        data={
            "_csrf_token": token(client),
            "workflow_id": workflow_id,
            "interval_minutes": "5",
        },
        follow_redirects=True,
    )
    assert schedule.status_code == 200
    assert b"Every 5 min" in schedule.data

    imported = client.post(
        "/workflows/import",
        data={
            "_csrf_token": token(client),
            "workflow_file": (io.BytesIO(json.dumps(definition).encode("utf-8")), "flow.json"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert imported.status_code == 200
    assert b"Workflow imported and validated." in imported.data

    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute(
            """INSERT INTO executions
               (execution_id, workflow_id, status, started_at)
               VALUES ('active-test', ?, 'running', '2026-01-01T00:00:00+00:00')""",
            (workflow_id,),
        )
    client.post(
        f"/workflows/{workflow_id}/delete",
        data={"_csrf_token": token(client)},
        follow_redirects=True,
    )
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT deleted_at FROM workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT enabled FROM schedules WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0] == 1
        connection.execute(
            "UPDATE executions SET status = 'failed' WHERE execution_id = 'active-test'"
        )
    client.post(
        f"/workflows/{workflow_id}/delete",
        data={"_csrf_token": token(client)},
        follow_redirects=True,
    )
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT deleted_at FROM workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0] is not None
        assert connection.execute(
            "SELECT enabled FROM schedules WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()[0] == 0


def test_settings_are_validated_and_persisted(client, app):
    response = client.post(
        "/settings",
        data={"_csrf_token": token(client), "max_parallel_tasks": "4"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT value FROM settings WHERE key = 'max_parallel_tasks'"
        ).fetchone()[0] == "4"
    restarted = create_app(
        {
            "TESTING": True,
            "START_SCHEDULER": False,
            "DATABASE": app.config["DATABASE"],
            "FILE_ROOT": app.config["FILE_ROOT"],
        }
    )
    assert restarted.config["MAX_PARALLEL_TASKS"] == 4
    restarted.extensions["workflowrunner_executor"].shutdown(wait=True)
