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


def test_failed_task_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "history.db"))
    storage.init_db()
    task_id = storage.create_task("Retry this task")
    storage.finish_task(task_id, "failed", "Temporary provider error")

    assert storage.restart_task(task_id) is True
    task = storage.get_task(task_id)
    assert task["status"] == "running"
    assert task["result"] is None
    assert storage.restart_task(task_id) is False


def test_recover_interrupted_tasks(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "history.db"))
    storage.init_db()
    task_id = storage.create_task("Interrupted task")

    assert storage.recover_interrupted_tasks() == 1
    task = storage.get_task(task_id)
    assert task["status"] == "failed"
    assert "server restart" in task["result"]
    assert task["active_agent"] is None


def test_failed_task_retains_active_agent_for_dashboard(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "history.db"))
    storage.init_db()
    task_id = storage.create_task("Fail during research")
    storage.set_active_agent(task_id, "research")

    storage.finish_task(task_id, "failed", "Provider error")

    task = storage.get_task(task_id)
    assert task["status"] == "failed"
    assert task["active_agent"] == "research"
