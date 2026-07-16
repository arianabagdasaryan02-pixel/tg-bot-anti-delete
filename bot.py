"""
Бот через Telegram Business API (облачный api.telegram.org).
Ловит правки и удаления в личных чатах.
Сохраняет медиа до 20 МБ локально, чтобы прислать оригинал при удалении.
Для больших файлов хранит file_id, метаданные и доступное превью/thumbnail.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    BusinessMessagesDeletedHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "messages.db"
ARCHIVE_DIR = Path("media_archive")
ARCHIVE_DIR.mkdir(exist_ok=True)

MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024  # 20 МБ — лимит облачного Bot API для getFile/download


# ---------- База ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            business_connection_id TEXT,
            chat_id INTEGER,
            message_id INTEGER,
            chat_title TEXT,
            sender_id INTEGER,
            sender_name TEXT,
            sender_username TEXT,
            text TEXT,
            media_type TEXT,
            media_file_id TEXT,
            media_unique_id TEXT,
            media_path TEXT,
            thumb_file_id TEXT,
            thumb_path TEXT,
            media_size INTEGER,
            media_duration INTEGER,
            too_big INTEGER DEFAULT 0,
            date TEXT,
            PRIMARY KEY (business_connection_id, chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS connections (
            business_connection_id TEXT PRIMARY KEY,
            user_id INTEGER,
            account_user_id INTEGER,
            is_enabled INTEGER,
            date TEXT
        );
        """
    )

    # Миграция старых SQLite-баз без удаления данных.
    message_columns = (
        "media_file_id TEXT",
        "media_unique_id TEXT",
        "media_path TEXT",
        "thumb_file_id TEXT",
        "thumb_path TEXT",
        "media_size INTEGER",
        "media_duration INTEGER",
        "too_big INTEGER DEFAULT 0",
    )
    for col in message_columns:
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    try:
        conn.execute("ALTER TABLE connections ADD COLUMN account_user_id INTEGER")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def save_message(**fields):
    defaults = {
        "bc_id": None,
        "chat_id": None,
        "message_id": None,
        "chat_title": "",
        "sender_id": 0,
        "sender_name": "?",
        "sender_username": "",
        "text": "",
        "media_type": None,
        "media_file_id": None,
        "media_unique_id": None,
        "media_path": None,
        "thumb_file_id": None,
        "thumb_path": None,
        "media_size": 0,
        "media_duration": None,
        "too_big": 0,
        "date": datetime.now().isoformat(),
    }
    defaults.update(fields)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT OR REPLACE INTO messages
        (business_connection_id, chat_id, message_id, chat_title,
         sender_id, sender_name, sender_username, text,
         media_type, media_file_id, media_unique_id, media_path,
         thumb_file_id, thumb_path, media_size, media_duration, too_big, date)
        VALUES (:bc_id, :chat_id, :message_id, :chat_title,
                :sender_id, :sender_name, :sender_username, :text,
                :media_type, :media_file_id, :media_unique_id, :media_path,
                :thumb_file_id, :thumb_path, :media_size, :media_duration, :too_big, :date)
        """,
        defaults,
    )
    conn.commit()
    conn.close()


def get_message(bc_id, chat_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT text, sender_name, sender_username, chat_title, "
        "media_type, media_file_id, media_unique_id, media_path, "
        "thumb_file_id, thumb_path, media_size, media_duration, too_big, sender_id "
        "FROM messages WHERE business_connection_id=? AND chat_id=? AND message_id=?",
        (bc_id, chat_id, message_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_deleted_batch(bc_id, chat_id, message_ids):
    if not message_ids:
        return []

    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(message_ids))
    cur = conn.execute(
        f"SELECT message_id, text, sender_name, sender_username, chat_title, "
        f"media_type, media_file_id, media_path, thumb_file_id, thumb_path, "
        f"media_size, media_duration, too_big, sender_id "
        f"FROM messages WHERE business_connection_id=? AND chat_id=? "
        f"AND message_id IN ({placeholders})",
        (bc_id, chat_id, *message_ids),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def _business_connection_owner_chat_id(bc):
    """Куда отправлять уведомления владельцу business-подключения."""
    if not bc:
        return None
    return getattr(bc, "user_chat_id", None) or (bc.user.id if getattr(bc, "user", None) else None)


def _business_connection_account_user_id(bc):
    """ID самого business-аккаунта, если библиотека его отдала."""
    return bc.user.id if bc and getattr(bc, "user", None) else None


def save_connection(bc_id, owner_chat_id, is_enabled, account_user_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT OR REPLACE INTO connections
        (business_connection_id, user_id, account_user_id, is_enabled, date)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            bc_id,
            owner_chat_id,
            account_user_id,
            int(bool(is_enabled)),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def cache_business_connection(bc):
    """Сохранить BusinessConnection и вернуть chat_id владельца для уведомлений."""
    owner_chat_id = _business_connection_owner_chat_id(bc)
    if owner_chat_id:
        save_connection(
            bc.id,
            owner_chat_id,
            getattr(bc, "is_enabled", True),
            _business_connection_account_user_id(bc),
        )
    return owner_chat_id


def get_owner(bc_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT user_id FROM connections WHERE business_connection_id=? AND is_enabled=1",
        (bc_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_account_user_id(bc_id):
    """ID business-аккаунта. Нужен, чтобы отличать входящие сообщения от своих."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT account_user_id, user_id FROM connections WHERE business_connection_id=? AND is_enabled=1",
        (bc_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return row[0] or row[1]


# ---------- Утилиты ----------

def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_text(msg):
    return msg.text or msg.caption or ""


# Атрибуты msg, которые могут содержать медиа, и есть ли у них duration/thumbnail.
MEDIA_ATTRS_WITH_DURATION = {"video", "video_note", "voice", "audio", "animation"}
MEDIA_ATTRS = (
    "video", "video_note", "voice", "audio", "animation", "document", "sticker",
)


def extract_media(msg):
    """Вернуть type, file_id, file_unique_id, thumb_file_id, size, duration."""
    info = {
        "type": None,
        "file_id": None,
        "unique_id": None,
        "thumb_file_id": None,
        "size": 0,
        "duration": None,
    }

    if msg.photo:
        best = msg.photo[-1]
        smallest = msg.photo[0] if len(msg.photo) > 1 else None
        info.update(
            type="photo",
            file_id=best.file_id,
            unique_id=best.file_unique_id,
            thumb_file_id=getattr(smallest, "file_id", None),
            size=best.file_size or 0,
        )
        return info

    for attr in MEDIA_ATTRS:
        m = getattr(msg, attr, None)
        if not m:
            continue
        info.update(type=attr, file_id=m.file_id, unique_id=m.file_unique_id, size=m.file_size or 0)
        if attr in MEDIA_ATTRS_WITH_DURATION:
            info["duration"] = getattr(m, "duration", None)
        thumb = getattr(m, "thumbnail", None)
        if thumb:
            info["thumb_file_id"] = thumb.file_id
        break

    return info


async def download(bot, file_id, media_type, bc_id, chat_id, msg_id, suffix=""):
    if not file_id:
        return None

    try:
        tg_file = await bot.get_file(file_id)
        dst = ARCHIVE_DIR / f"{bc_id}_{chat_id}_{msg_id}_{media_type}{suffix}"
        await tg_file.download_to_drive(custom_path=str(dst))
        return str(dst)
    except BadRequest as e:
        logger.info("Файл не скачан через облачный Bot API: %s", e)
        return None
    except Exception as e:
        logger.warning("Не удалось скачать %s: %s", file_id, e)
        return None


async def archive_media_if_possible(bot, info, bc_id, chat_id, msg_id):
    """Скачать оригинал до 20 МБ и thumbnail/preview, если Telegram его отдал."""
    media_path = None
    thumb_path = None
    too_big = 0

    if info["file_id"]:
        if info["size"] and info["size"] > MAX_DOWNLOAD_SIZE:
            too_big = 1
        else:
            media_path = await download(
                bot,
                info["file_id"],
                info["type"],
                bc_id,
                chat_id,
                msg_id,
            )
            if media_path is None and info["size"] > 0:
                too_big = 1

        if info["thumb_file_id"]:
            thumb_path = await download(
                bot,
                info["thumb_file_id"],
                info["type"],
                bc_id,
                chat_id,
                msg_id,
                suffix="_thumb.jpg",
            )

    return media_path, thumb_path, too_big


def fmt_size(size_bytes):
    if not size_bytes:
        return ""
    size = float(size_bytes)
    for unit in ["Б", "КБ", "МБ", "ГБ"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def fmt_duration(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}ч {m}м {s}с"
    return f"{m}м {s}с"


def media_label(media_type):
    return {
        "photo": "📷 фото",
        "video": "🎬 видео",
        "video_note": "⭕ кружок",
        "voice": "🎤 голосовое",
        "audio": "🎵 аудио",
        "animation": "🎞 гифка",
        "document": "📄 документ",
        "sticker": "🎨 стикер",
    }.get(media_type, media_type)


# Официальный аккаунт Telegram с системными уведомлениями (коды входа и т.п.).
# В клиенте отображается как Telegram / Service notifications, id всегда 777000.
TELEGRAM_SERVICE_ACCOUNT_ID = 777000


def _norm_username(username) -> str:
    return (username or "").lstrip("@").lower()


def _chat_username(chat) -> str:
    return _norm_username(getattr(chat, "username", None))


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_telegram_service_account(chat=None, user=None) -> bool:
    """Официальный аккаунт Telegram Service notifications (id 777000)."""
    for obj in (user, chat):
        if obj and _safe_int(getattr(obj, "id", None)) == TELEGRAM_SERVICE_ACCOUNT_ID:
            return True
    return False


def _looks_like_bot_username(username) -> bool:
    uname = _norm_username(username)
    return bool(uname and uname.endswith("bot"))


def _is_target_private_chat(chat) -> bool:
    """Бот должен следить только за личными чатами."""
    chat_type = getattr(chat, "type", None)
    return bool(chat and str(chat_type).lower().endswith("private"))


def _user_link(user_id, name, username=""):
    """
    Безопасное отображение автора.
    Для пользователей с username НЕ создаём HTML-ссылку https://t.me/username,
    иначе Telegram добавляет превью карточки снизу. Достаточно обычного @username:
    Telegram сам сделает его кликабельным.
    """
    try:
        uid = int(user_id) if user_id else 0
    except (TypeError, ValueError):
        uid = 0

    display = html_escape(name or "?")
    uname = _norm_username(username)

    if uname:
        return f"{display} (@{html_escape(uname)})"

    if uid:
        return f'<a href="tg://user?id={uid}">{display}</a>'

    return display


def _is_message_from_bot(msg) -> bool:
    """Главная проверка для business_message / edited_business_message."""
    user = getattr(msg, "from_user", None)
    return bool(user and getattr(user, "is_bot", False))


def _should_ignore_business_message(msg, owner_id) -> bool:
    """Единый фильтр для новых и отредактированных business-сообщений."""
    chat = getattr(msg, "chat", None)

    if not _is_target_private_chat(chat):
        return True

    if _is_telegram_service_account(chat, getattr(msg, "from_user", None)):
        return True

    # Основной надёжный критерий — from_user.is_bot.
    if _is_message_from_bot(msg):
        return True

    # Fallback: на случай некорректной обёртки/API-адаптера.
    if _looks_like_bot_username(_chat_username(chat)):
        return True

    # Для задачи важны только сообщения реальных пользователей.
    if not getattr(msg, "from_user", None):
        return True

    if _is_own_message(msg, owner_id):
        return True

    return False


def _should_ignore_deleted_event(ev) -> bool:
    """Фильтр для deleted_business_messages, где нет from_user."""
    chat = getattr(ev, "chat", None)

    if not _is_target_private_chat(chat):
        return True

    if _is_telegram_service_account(chat, None):
        return True

    if _looks_like_bot_username(_chat_username(chat)):
        return True

    return False


def _is_own_message(msg, owner_id) -> bool:
    """Сообщение отправил владелец аккаунта (я сам)?"""
    return bool(owner_id and msg.from_user and msg.from_user.id == owner_id)


async def ensure_owner(context, bc_id):
    """
    Вернуть chat_id владельца business-подключения.
    Если бот пропустил business_connection update или база была очищена,
    пробуем заново получить BusinessConnection через Bot API.
    """
    owner = get_owner(bc_id)
    if owner:
        return owner

    getter = getattr(context.bot, "get_business_connection", None)
    if not getter:
        logger.warning(
            "Business connection %s is not cached, and this python-telegram-bot "
            "version has no get_business_connection() method",
            bc_id,
        )
        return None

    try:
        bc = await getter(bc_id)
    except Exception as e:
        logger.warning("Could not fetch business connection %s: %s", bc_id, e)
        return None

    owner = cache_business_connection(bc)
    if owner:
        logger.info(
            "Business connection fetched and cached: bc_id=%s owner_chat_id=%s account_user_id=%s",
            bc_id,
            owner,
            _business_connection_account_user_id(bc),
        )
    return owner


# ---------- Хендлеры ----------

# Имя бота подставляется из config.BOT_USERNAME (без @).
# Пример: BOT_USERNAME = "my_awesome_bot"
BOT_USERNAME = getattr(config, "BOT_USERNAME", "your_bot_username")

WELCOME_TEXT = (
    "<b>Что умеет этот бот?</b>\n"
    "🗑 Следить за <b>удалением</b> сообщений — если кто-то удалит сообщение, "
    "бот пришлёт вам его копию.\n"
    "✏️ Следить за <b>изменением</b> сообщений — бот пришлёт вам исходное сообщение, "
    "если его отредактируют.\n"
    "📷 Сохранять удалённые фото, голосовые и кружочки.\n\n"
    "<b>Как подключить бота к аккаунту:</b>\n"
    "1. Перейди в свой профиль\n"
    "2. Нажми «Изменить»\n"
    "3. Выбери «Автоматизация чатов»\n"
    "4. Найдите и добавьте бота:\n"
    f"<blockquote>@{BOT_USERNAME}</blockquote>\n\n"
    "<i>Необходима последняя версия Telegram</i>"
)

WELCOME_VIDEO = "instruction.mp4"  # видео-инструкция, положить рядом с bot.py, необязательно
WELCOME_GIF = "instruction.gif"    # gif-инструкция (используется, если нет mp4), необязательно
WELCOME_IMAGE = "welcome.png"      # положить рядом с bot.py, необязательно


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Видео-инструкция (mp4)
    if os.path.exists(WELCOME_VIDEO):
        try:
            with open(WELCOME_VIDEO, "rb") as vid:
                await update.message.reply_video(
                    video=vid,
                    caption=WELCOME_TEXT,
                    parse_mode="HTML",
                    supports_streaming=True,
                )
                return
        except Exception as e:
            logger.warning("Не удалось отправить видео-инструкцию: %s", e)

    # 2. Gif-инструкция (если видео нет или не отправилось)
    if os.path.exists(WELCOME_GIF):
        try:
            with open(WELCOME_GIF, "rb") as gif:
                await update.message.reply_animation(
                    animation=gif,
                    caption=WELCOME_TEXT,
                    parse_mode="HTML",
                )
                return
        except Exception as e:
            logger.warning("Не удалось отправить gif-инструкцию: %s", e)

    # 3. Статичное фото
    if os.path.exists(WELCOME_IMAGE):
        try:
            with open(WELCOME_IMAGE, "rb") as img:
                await update.message.reply_photo(
                    photo=img,
                    caption=WELCOME_TEXT,
                    parse_mode="HTML",
                )
                return
        except Exception as e:
            logger.warning("Не удалось отправить приветственное фото: %s", e)

    # 4. Просто текст
    await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML")


async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    owner_chat_id = cache_business_connection(bc)
    account_user_id = _business_connection_account_user_id(bc)

    if bc.is_enabled and owner_chat_id:
        logger.info(
            "Business connection: owner_chat_id=%s account_user_id=%s bc_id=%s",
            owner_chat_id,
            account_user_id,
            bc.id,
        )
        try:
            await context.bot.send_message(
                chat_id=owner_chat_id,
                text="Ботик подключен и уведомит вас об удалённых и изменённых сообщениях.",
            )
        except Exception:
            pass


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.business_message
    if not msg:
        return

    owner = await ensure_owner(context, msg.business_connection_id)
    if not owner:
        return

    account_user_id = get_account_user_id(msg.business_connection_id) or owner
    if _should_ignore_business_message(msg, account_user_id):
        return

    info = extract_media(msg)
    media_path, thumb_path, too_big = await archive_media_if_possible(
        context.bot,
        info,
        msg.business_connection_id,
        msg.chat.id,
        msg.message_id,
    )

    user = msg.from_user
    save_message(
        bc_id=msg.business_connection_id,
        chat_id=msg.chat.id,
        message_id=msg.message_id,
        chat_title=msg.chat.title or msg.chat.full_name or str(msg.chat.id),
        sender_id=user.id if user else 0,
        sender_name=user.full_name if user else "?",
        sender_username=(user.username if user else "") or "",
        text=extract_text(msg),
        media_type=info["type"],
        media_file_id=info["file_id"],
        media_unique_id=info["unique_id"],
        media_path=media_path,
        thumb_file_id=info["thumb_file_id"],
        thumb_path=thumb_path,
        media_size=info["size"],
        media_duration=info["duration"],
        too_big=too_big,
        date=datetime.now().isoformat(),
    )


async def on_edited_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_business_message
    if not msg:
        return

    owner = await ensure_owner(context, msg.business_connection_id)
    if not owner:
        return

    account_user_id = get_account_user_id(msg.business_connection_id) or owner
    if _should_ignore_business_message(msg, account_user_id):
        return

    new_text = extract_text(msg)
    new_info = extract_media(msg)
    old = get_message(msg.business_connection_id, msg.chat.id, msg.message_id)

    old_media_file_id = None
    old_media_unique_id = None
    old_media_path = None
    old_thumb_file_id = None
    old_thumb_path = None
    old_too_big = 0

    if old and owner:
        (
            old_text,
            old_name,
            old_username,
            _chat_title,
            old_media_type,
            old_media_file_id,
            old_media_unique_id,
            old_media_path,
            old_thumb_file_id,
            old_thumb_path,
            _old_size,
            _old_dur,
            old_too_big,
            old_sender_id,
        ) = old

        text_changed = (old_text or "") != new_text
        media_changed = (old_media_unique_id or "") != (new_info["unique_id"] or "")

        if text_changed or media_changed:
            user = msg.from_user
            author = (user.full_name if user else None) or old_name or "?"
            uname = (user.username if user else None) or old_username or ""
            sender_id = (user.id if user else None) or old_sender_id or 0
            author_link = _user_link(sender_id, author, uname)

            title = "🖼 Медиа изменено" if media_changed else "✏️ Изменённое сообщение"
            report = (
                f"<b>{title}</b>\n"
                f"👤 {author_link}\n"
            )

            if media_changed:
                old_label = media_label(old_media_type) if old_media_type else "текст"
                new_label = media_label(new_info["type"]) if new_info["type"] else "текст"
                report += f"\n🔄 Файл заменён: {old_label} → {new_label}\n"
                if old_too_big:
                    report += "<i>Файл больше 20 МБ — отправлен без локальной копии.</i>\n"

            if text_changed or old_text or new_text:
                report += (
                    f"\n<b>Было:</b>\n<blockquote>{html_escape(old_text or '[пусто]')}</blockquote>\n"
                    f"<b>Стало:</b>\n<blockquote>{html_escape(new_text or '[пусто]')}</blockquote>"
                )

            try:
                if media_changed and old_media_type:
                    await _send_stored_media(
                        context.bot,
                        owner,
                        old_media_type,
                        old_media_path,
                        old_media_file_id,
                        old_thumb_path,
                        old_thumb_file_id,
                        report,
                    )
                else:
                    await _send_text_report(context.bot, owner, report)
            except Exception as e:
                logger.exception("send edit report failed: %s", e)
                try:
                    await _send_text_report(context.bot, owner, report)
                except Exception:
                    pass

    # Обновляем кэш сообщения после обработки изменения.
    user = msg.from_user
    media_changed_for_cache = True
    if old:
        media_changed_for_cache = (old_media_unique_id or "") != (new_info["unique_id"] or "")

    if old and not media_changed_for_cache:
        media_file_id = old_media_file_id
        media_path = old_media_path
        thumb_file_id = old_thumb_file_id
        thumb_path = old_thumb_path
        too_big = old_too_big
    elif new_info["file_id"]:
        media_file_id = new_info["file_id"]
        thumb_file_id = new_info["thumb_file_id"]
        media_path, thumb_path, too_big = await archive_media_if_possible(
            context.bot,
            new_info,
            msg.business_connection_id,
            msg.chat.id,
            msg.message_id,
        )
    else:
        media_file_id = None
        media_path = None
        thumb_file_id = None
        thumb_path = None
        too_big = 0

    save_message(
        bc_id=msg.business_connection_id,
        chat_id=msg.chat.id,
        message_id=msg.message_id,
        chat_title=msg.chat.title or msg.chat.full_name or str(msg.chat.id),
        sender_id=user.id if user else 0,
        sender_name=user.full_name if user else "?",
        sender_username=(user.username if user else "") or "",
        text=new_text,
        media_type=new_info["type"],
        media_file_id=media_file_id,
        media_unique_id=new_info["unique_id"],
        media_path=media_path,
        thumb_file_id=thumb_file_id,
        thumb_path=thumb_path,
        media_size=new_info["size"],
        media_duration=new_info["duration"],
        too_big=too_big,
        date=datetime.now().isoformat(),
    )


# ---------- Отправка отчётов ----------

async def _send_text_report(bot, chat_id, text):
    """Отправить текстовый отчёт без web preview."""
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _caption_kwargs_or_none(caption):
    # Подпись к медиа короче обычного сообщения. Если отчёт длинный —
    # шлём его отдельным текстом, а файл прикладываем без подписи.
    if not caption or len(caption) > 1000:
        return None
    return {"caption": caption, "parse_mode": "HTML"}


async def _send_media(bot, chat_id, media_type, source, caption):
    """
    Отправить медиа с подписью (или отдельным текстом, если нельзя).
    `source` — открытый файловый объект (локальный оригинал) или file_id (строка).
    """
    caption_kwargs = _caption_kwargs_or_none(caption)
    if caption_kwargs is None:
        await _send_text_report(bot, chat_id, caption)
        caption_kwargs = {}

    senders = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "voice": bot.send_voice,
        "audio": bot.send_audio,
        "animation": bot.send_animation,
    }
    sender = senders.get(media_type)
    if sender:
        await sender(chat_id, source, **caption_kwargs)
        return

    if media_type in ("video_note", "sticker"):
        if caption_kwargs:
            await _send_text_report(bot, chat_id, caption)
        if hasattr(source, "seek"):
            source.seek(0)
        if media_type == "video_note":
            await bot.send_video_note(chat_id, source)
        else:
            await bot.send_sticker(chat_id, source)
        return

    await bot.send_document(chat_id, source, **caption_kwargs)


async def _send_stored_media(bot, chat_id, media_type, media_path, media_file_id,
                              thumb_path, thumb_file_id, caption):
    """
    Порядок отправки:
    1. локально сохранённый оригинал;
    2. оригинал по file_id, если файл был больше 20 МБ или не скачался;
    3. локальное preview/thumbnail;
    4. preview/thumbnail по file_id;
    5. только текстовый отчёт.
    """
    if media_path and os.path.exists(media_path):
        try:
            with open(media_path, "rb") as f:
                await _send_media(bot, chat_id, media_type, f, caption)
            return
        except Exception as e:
            logger.warning("Не отправил локальное медиа %s: %s", media_path, e)

    if media_file_id:
        try:
            await _send_media(bot, chat_id, media_type, media_file_id, caption)
            return
        except Exception as e:
            logger.warning("Не отправил медиа по file_id: %s", e)

    if thumb_path and os.path.exists(thumb_path):
        try:
            with open(thumb_path, "rb") as f:
                await _send_media(bot, chat_id, "photo", f, caption)
            return
        except Exception as e:
            logger.warning("Не отправил локальный thumbnail %s: %s", thumb_path, e)

    if thumb_file_id:
        try:
            await _send_media(bot, chat_id, "photo", thumb_file_id, caption)
            return
        except Exception as e:
            logger.warning("Не отправил thumbnail по file_id: %s", e)

    await _send_text_report(bot, chat_id, caption)


async def on_deleted_business_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = update.deleted_business_messages
    if not ev or not ev.message_ids:
        return

    if _should_ignore_deleted_event(ev):
        return

    owner = await ensure_owner(context, ev.business_connection_id)
    if not owner:
        return

    rows = get_deleted_batch(ev.business_connection_id, ev.chat.id, ev.message_ids)
    if not rows:
        return

    for row in rows:
        (
            _mid,
            text,
            sender_name,
            sender_username,
            _chat_title,
            media_type,
            media_file_id,
            media_path,
            thumb_file_id,
            thumb_path,
            media_size,
            media_duration,
            too_big,
            sender_id,
        ) = row

        # Защита для старых записей в БД, которые могли попасть туда до обновления фильтра.
        if _safe_int(sender_id) == TELEGRAM_SERVICE_ACCOUNT_ID or _looks_like_bot_username(sender_username):
            continue

        author_link = _user_link(sender_id, sender_name or "?", sender_username or "")
        caption = (
            f"🗑 <b>Удалённое сообщение</b>\n"
            f"👤 {author_link}\n"
        )
        if text:
            caption += f"\n<b>Текст:</b>\n<blockquote>{html_escape(text)}</blockquote>\n"
        if media_type:
            line = f"\n📎 <b>Медиа:</b> {media_label(media_type)}"
            if media_size:
                line += f" · {fmt_size(media_size)}"
            if media_duration:
                line += f" · {fmt_duration(media_duration)}"
            caption += line
            if too_big:
                caption += "\n<i>Файл больше 20 МБ — отправлен без локальной копии.</i>"

        try:
            if media_type:
                await _send_stored_media(
                    context.bot,
                    owner,
                    media_type,
                    media_path,
                    media_file_id,
                    thumb_path,
                    thumb_file_id,
                    caption,
                )
            else:
                await _send_text_report(context.bot, owner, caption)
        except Exception as e:
            logger.exception("send delete report failed: %s", e)
            try:
                await _send_text_report(context.bot, owner, caption)
            except Exception:
                pass


# ---------- Запуск ----------

def main():
    init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, on_edited_business_message))
    app.add_handler(BusinessMessagesDeletedHandler(on_deleted_business_messages))

    logger.info("Бот запущен: Business API, архив до 20 МБ, file_id/preview для больших файлов.")
    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ]
    )


if __name__ == "__main__":
    main()
