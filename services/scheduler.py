"""Small local interval scheduler backed by the application's SQLite database."""

from datetime import datetime, timedelta, timezone
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from database import db_session

logger = logging.getLogger(__name__)


def run_due_schedules(database: str | Path, launch: Callable[[str], str]) -> int:
    now = datetime.now(timezone.utc)
    with db_session(database) as connection:
        rows = connection.execute(
            """SELECT schedule_id, workflow_id, interval_minutes, next_run_at
               FROM schedules WHERE enabled = 1 AND next_run_at <= ?
               ORDER BY next_run_at, schedule_id""",
            (now.isoformat(),),
        ).fetchall()
        claimed: list[tuple[str, str, int]] = []
        for row in rows:
            next_run = now + timedelta(minutes=row["interval_minutes"])
            updated = connection.execute(
                """UPDATE schedules SET next_run_at = ?
                   WHERE schedule_id = ? AND next_run_at = ? AND enabled = 1""",
                (next_run.isoformat(), row["schedule_id"], row["next_run_at"]),
            ).rowcount
            if updated:
                claimed.append(
                    (row["schedule_id"], row["workflow_id"], row["interval_minutes"])
                )
    for schedule_id, workflow_id, _ in claimed:
        try:
            execution_id = launch(workflow_id)
        except (ValueError, RuntimeError) as exc:
            logger.warning("Schedule %s could not launch: %s", schedule_id, exc)
            continue
        with db_session(database) as connection:
            connection.execute(
                "UPDATE schedules SET last_execution_id = ? WHERE schedule_id = ?",
                (execution_id, schedule_id),
            )
    return len(claimed)


class LocalScheduler:
    def __init__(
        self,
        database: str | Path,
        launch: Callable[[str], str],
        interval_seconds: int = 10,
    ) -> None:
        self.database = database
        self.launch = launch
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(self.interval_seconds):
                try:
                    run_due_schedules(self.database, self.launch)
                except sqlite3.Error:
                    logger.exception("The local scheduler could not access its database.")

        self._thread = threading.Thread(
            target=loop, name="workflowrunner-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.interval_seconds + 1)
