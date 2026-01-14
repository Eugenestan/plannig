"""
Миграция SQLite: переход с credential_id -> app_user_id для пользовательских таблиц.

Почему нужно:
- В текущей planing.db таблицы `improve_task_order`, `gantt_state`, `todo_lists`, `todo_tasks`
  всё ещё имеют колонку `credential_id NOT NULL`.
- Код уже пишет через `app_user_id`, из-за чего возникает:
  sqlite3.IntegrityError: NOT NULL constraint failed: <table>.credential_id

Что делает миграция:
- Создаёт новые таблицы без `credential_id` и с `app_user_id NOT NULL`
- Переносит данные, вычисляя app_user_id через join на api_credentials (id = credential_id)
- Сохраняет исходные id строк, чтобы не ломать связи (todo_subtasks -> todo_tasks)

Запуск:
  python -m app.migrate_sqlite_app_user_id
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _table_has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _app_user_id_expr(cur: sqlite3.Cursor, table: str, alias: str) -> str:
    """
    SQLite миграции могут выполняться на БД разных версий:
    - в некоторых таблицах уже есть колонка app_user_id (nullable) после частичных миграций
    - в некоторых её ещё нет (только credential_id)
    Возвращаем SQL-выражение, которое безопасно вычисляет app_user_id.
    """
    if _table_has_column(cur, table, "app_user_id"):
        return f"COALESCE({alias}.app_user_id, c.app_user_id)"
    return "c.app_user_id"


def _migrate_improve_task_order(cur: sqlite3.Cursor) -> None:
    if not _table_has_column(cur, "improve_task_order", "credential_id"):
        return

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS improve_task_order_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_user_id INTEGER NOT NULL,
            task_key VARCHAR(64) NOT NULL,
            position INTEGER NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_improve_task_order UNIQUE (app_user_id, task_key)
        )
        """
    )

    # переносим данные, сохраняя id
    app_user_expr = _app_user_id_expr(cur, "improve_task_order", "o")
    cur.execute(
        """
        INSERT OR IGNORE INTO improve_task_order_new (id, app_user_id, task_key, position, created_at, updated_at)
        SELECT
            o.id,
            {app_user_expr} AS app_user_id,
            o.task_key,
            o.position,
            o.created_at,
            o.updated_at
        FROM improve_task_order o
        LEFT JOIN api_credentials c ON c.id = o.credential_id
        WHERE {app_user_expr} IS NOT NULL
        """
        .format(app_user_expr=app_user_expr)
    )

    cur.execute("DROP TABLE improve_task_order")
    cur.execute("ALTER TABLE improve_task_order_new RENAME TO improve_task_order")


def _migrate_gantt_state(cur: sqlite3.Cursor) -> None:
    if not _table_has_column(cur, "gantt_state", "credential_id"):
        return

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS gantt_state_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_user_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            state_data VARCHAR(10000) NOT NULL,
            auto_mode BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_gantt_state UNIQUE (app_user_id, team_id)
        )
        """
    )

    app_user_expr = _app_user_id_expr(cur, "gantt_state", "s")
    cur.execute(
        """
        INSERT OR IGNORE INTO gantt_state_new (id, app_user_id, team_id, state_data, auto_mode, created_at, updated_at)
        SELECT
            s.id,
            {app_user_expr} AS app_user_id,
            s.team_id,
            s.state_data,
            s.auto_mode,
            s.created_at,
            s.updated_at
        FROM gantt_state s
        LEFT JOIN api_credentials c ON c.id = s.credential_id
        WHERE {app_user_expr} IS NOT NULL
        """
        .format(app_user_expr=app_user_expr)
    )

    cur.execute("DROP TABLE gantt_state")
    cur.execute("ALTER TABLE gantt_state_new RENAME TO gantt_state")


def _migrate_todo_lists(cur: sqlite3.Cursor) -> None:
    if not _table_has_column(cur, "todo_lists", "credential_id"):
        return

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS todo_lists_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_user_id INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    app_user_expr = _app_user_id_expr(cur, "todo_lists", "l")
    cur.execute(
        """
        INSERT OR IGNORE INTO todo_lists_new (id, app_user_id, name, position, created_at, updated_at)
        SELECT
            l.id,
            {app_user_expr} AS app_user_id,
            l.name,
            l.position,
            l.created_at,
            l.updated_at
        FROM todo_lists l
        LEFT JOIN api_credentials c ON c.id = l.credential_id
        WHERE {app_user_expr} IS NOT NULL
        """
        .format(app_user_expr=app_user_expr)
    )

    cur.execute("DROP TABLE todo_lists")
    cur.execute("ALTER TABLE todo_lists_new RENAME TO todo_lists")


def _migrate_todo_tasks(cur: sqlite3.Cursor) -> None:
    if not _table_has_column(cur, "todo_tasks", "credential_id"):
        return

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS todo_tasks_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_user_id INTEGER NOT NULL,
            list_id INTEGER NULL,
            list_type VARCHAR(50) NULL,
            name VARCHAR(500) NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT 0,
            priority VARCHAR(20) NOT NULL DEFAULT 'normal',
            due_date DATETIME NULL,
            reminder DATETIME NULL,
            repeat VARCHAR(20) NULL,
            notes TEXT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    app_user_expr = _app_user_id_expr(cur, "todo_tasks", "t")
    cur.execute(
        """
        INSERT OR IGNORE INTO todo_tasks_new (
            id, app_user_id, list_id, list_type, name, completed, priority, due_date, reminder, repeat, notes, position, created_at, updated_at
        )
        SELECT
            t.id,
            {app_user_expr} AS app_user_id,
            t.list_id,
            t.list_type,
            t.name,
            t.completed,
            t.priority,
            t.due_date,
            t.reminder,
            t.repeat,
            t.notes,
            t.position,
            t.created_at,
            t.updated_at
        FROM todo_tasks t
        LEFT JOIN api_credentials c ON c.id = t.credential_id
        WHERE {app_user_expr} IS NOT NULL
        """
        .format(app_user_expr=app_user_expr)
    )

    cur.execute("DROP TABLE todo_tasks")
    cur.execute("ALTER TABLE todo_tasks_new RENAME TO todo_tasks")


def run(db_path: Path) -> None:
    if not db_path.exists():
        raise SystemExit(f"DB file not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()

        # В SQLite FK могут мешать DROP/RENAME, временно выключаем
        cur.execute("PRAGMA foreign_keys=OFF")

        _migrate_improve_task_order(cur)
        _migrate_gantt_state(cur)
        _migrate_todo_lists(cur)
        _migrate_todo_tasks(cur)

        cur.execute("PRAGMA foreign_keys=ON")
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    # backend/planing.db
    backend_dir = Path(__file__).resolve().parent.parent
    db_path = backend_dir / "planing.db"
    run(db_path)
    print("OK: sqlite migration finished")

