import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = os.getenv("APP_DB_PATH", "./data/multiagent.db")

def connect():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_message TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            agent TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        """)

def create_task(message: str) -> int:
    with connect() as db:
        cur = db.execute("INSERT INTO tasks(user_message,status,created_at) VALUES(?,?,?)", (message, "running", datetime.now(timezone.utc).isoformat()))
        return int(cur.lastrowid)

def add_message(task_id: int, agent: str, content: str):
    with connect() as db:
        db.execute("INSERT INTO messages(task_id,agent,content,created_at) VALUES(?,?,?,?)", (task_id, agent, content, datetime.now(timezone.utc).isoformat()))

def finish_task(task_id: int, status: str, result: str):
    with connect() as db:
        db.execute("UPDATE tasks SET status=?, result=? WHERE id=?", (status, result, task_id))

def recent_tasks(limit: int = 30):
    with connect() as db:
        rows = db.execute("SELECT id,user_message,status,result,created_at FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

def task_messages(task_id: int):
    with connect() as db:
        rows = db.execute("SELECT agent,content,created_at FROM messages WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]
