import sqlite3

from app import storage


def test_sqlite_storage_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "history.db"))

    storage.init_db()
    task_id = storage.create_task("Research an idea")
    storage.add_message(task_id, "research", "Initial findings")
    storage.finish_task(task_id, "completed", "Final answer")

    tasks = storage.recent_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["result"] == "Final answer"

    messages = storage.task_messages(task_id)
    assert len(messages) == 1
    assert messages[0]["agent"] == "research"
    assert messages[0]["content"] == "Initial findings"


def test_sqlite_foreign_key_cascade_is_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "history.db"))
    storage.init_db()
    task_id = storage.create_task("Temporary task")
    storage.add_message(task_id, "writer", "Draft")

    with storage.connect() as db:
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    assert storage.task_messages(task_id) == []
