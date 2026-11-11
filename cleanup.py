"""
Ежедневная очистка.

Бот не держит у себя файлы, а тексты помнит недолго: этот скрипт
раз в день стирает всё, что старше DAYS_TO_KEEP, и сжимает базу.
Заодно подчищает временную папку media_archive, если в ней что-то
застряло после сорвавшейся отправки.
Запускается из cron, см. README.
"""
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta

# Сколько суток хранить. По умолчанию 1: бот помнит переписку не дольше суток.
# Чтобы хранить дольше, допишите в config.py строку вида  DAYS_TO_KEEP = 7
# Так настройка переживает обновления: config.py в репозиторий не попадает.
try:
    from config import DAYS_TO_KEEP
except Exception:
    DAYS_TO_KEEP = 1

# Файлы, которым меньше этого числа минут, не трогаем: возможно, прямо
# сейчас идёт отправка.
SKIP_FRESHER_THAN_MIN = 10

# Пути считаем от самого скрипта, чтобы корректно работать из cron.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "messages.db")
MEDIA_DIR = os.path.join(BASE_DIR, "media_archive")

cutoff = (datetime.now() - timedelta(days=DAYS_TO_KEEP)).isoformat()
stamp = datetime.now().isoformat(timespec="seconds")

try:
    conn = sqlite3.connect(DB_PATH)
    deleted_rows = conn.execute(
        "DELETE FROM messages WHERE date < ?", (cutoff,)
    ).rowcount
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"[{stamp}] Удалено записей: {deleted_rows}")
except Exception as e:
    print(f"[{stamp}] Ошибка при очистке базы: {e}")

# media_archive нужна боту на один миг: когда пересылка по file_id не прошла,
# он скачивает копию, отправляет её и сразу удаляет. Если бота в этот момент
# прибили, копия могла остаться. Убираем остатки и саму папку, чтобы в
# обычном состоянии её вообще не было.
if os.path.isdir(MEDIA_DIR):
    fresh_limit = time.time() - SKIP_FRESHER_THAN_MIN * 60
    removed = 0
    for name in os.listdir(MEDIA_DIR):
        path = os.path.join(MEDIA_DIR, name)
        try:
            if os.path.getmtime(path) > fresh_limit:
                continue  # возможно, отправка ещё идёт
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except Exception as e:
            print(f"[{stamp}] Не удалось удалить {name}: {e}")
    if removed:
        print(f"[{stamp}] Убрано временных файлов: {removed}")
    try:
        os.rmdir(MEDIA_DIR)  # сработает, только если папка опустела
        print(f"[{stamp}] Папка media_archive пуста, удалена")
    except OSError:
        pass
