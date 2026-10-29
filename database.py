"""SQLite connection and schema management."""

from contextlib import contextmanager
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    config_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, task_id)
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_executions_workflow_started
    ON executions(workflow_id, started_at DESC);
CREATE TABLE IF NOT EXISTS execution_tasks (
    execution_task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds REAL,
    output_json TEXT,
    error TEXT,
    UNIQUE(execution_id, task_id)
);
CREATE TABLE IF NOT EXISTS workflow_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    interval_minutes INTEGER NOT NULL,
    next_run_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_execution_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(database: str | Path) -> sqlite3.Connection:
    """Create a row-oriented connection with referential integrity enabled."""
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def init_db(database: str | Path) -> None:
    """Create the current schema and mark interrupted work after a restart."""
    with db_session(database) as connection:
        connection.executescript(SCHEMA)
        workflow_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(workflows)")
        }
        if "deleted_at" not in workflow_columns:
            connection.execute("ALTER TABLE workflows ADD COLUMN deleted_at TEXT")
        recovery_time = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """UPDATE executions
               SET status = 'interrupted', ended_at = COALESCE(ended_at, ?),
                   error = 'Application restarted while execution was active.'
               WHERE status IN ('queued', 'running')""",
            (recovery_time,),
        )
        connection.execute(
            """UPDATE execution_tasks SET status = 'interrupted',
                   ended_at = ?, error = 'Application restarted while task was active.'
               WHERE status = 'running'"""
            , (recovery_time,)
        )


@contextmanager
def db_session(database: str | Path) -> Generator[sqlite3.Connection, None, None]:
    """Yield a connection and commit or roll back the current transaction."""
    connection = connect(database)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
