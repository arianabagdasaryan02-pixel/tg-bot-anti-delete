"""
Очистка данных старше N дней:
- удаляет медиафайлы с диска
- удаляет записи из БД
Запуск раз в сутки через cron.
"""
import os
import sqlite3
from datetime import datetime, timedelta

DAYS_TO_KEEP = 7

# Путь считаем от расположения скрипта, чтобы работать из cron.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "messages.db")

cutoff = (datetime.now() - timedelta(days=DAYS_TO_KEEP)).isoformat()
deleted_files = 0
freed_bytes = 0

try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT media_path, thumb_path FROM messages WHERE date < ?", (cutoff,)
    )
    for media_path, thumb_path in cur.fetchall():
        for p in (media_path, thumb_path):
            if p and os.path.exists(p):
                try:
                    size = os.path.getsize(p)
                    os.remove(p)
                    deleted_files += 1
                    freed_bytes += size
                except Exception as e:
                    print(f"Не удалось удалить {p}: {e}")

    cur = conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
    deleted_rows = cur.rowcount
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    print(f"[{datetime.now().isoformat(timespec='seconds')}] "
          f"БД: {deleted_rows} записей; файлов: {deleted_files}; "
          f"освобождено {freed_bytes / 1024 / 1024:.1f} МБ")
except Exception as e:
    print(f"Ошибка: {e}")
