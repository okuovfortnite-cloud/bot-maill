"""A small Telegram bot that hands out unique email addresses from a queue."""

import asyncio
import hmac
import logging
import os
import re
import sqlite3
from pathlib import Path

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "emails.db"
GET_EMAIL_TEXT = "📧 Получить почту"


def get_admin_ids() -> set[int]:
    raw_ids = os.environ.get("ADMIN_IDS", "")
    try:
        return {int(value.strip()) for value in raw_ids.split(",") if value.strip()}
    except ValueError as error:
        raise RuntimeError("ADMIN_IDS должен содержать числовой Telegram ID") from error


ADMIN_IDS = get_admin_ids()


def get_access_password() -> str:
    password = os.environ.get("ACCESS_PASSWORD", "")
    if not password:
        raise RuntimeError("Укажите ACCESS_PASSWORD в переменных окружения.")
    return password


ACCESS_PASSWORD = get_access_password()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS emails (
                email TEXT PRIMARY KEY,
                issued_to INTEGER,
                issued_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                allowed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def add_emails(emails: list[str]) -> tuple[int, int]:
    added = 0
    duplicates = 0
    with connect() as connection:
        for email in emails:
            try:
                connection.execute("INSERT INTO emails (email) VALUES (?)", (email,))
                added += 1
            except sqlite3.IntegrityError:
                duplicates += 1
    return added, duplicates


def issue_email(user_id: int) -> str | None:
    # BEGIN IMMEDIATE makes selecting and marking the address one atomic operation.
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT email FROM emails WHERE issued_to IS NULL ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        email = row["email"]
        connection.execute(
            "UPDATE emails SET issued_to = ?, issued_at = CURRENT_TIMESTAMP WHERE email = ?",
            (user_id, email),
        )
        return email


def remaining_count() -> int:
    with connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM emails WHERE issued_to IS NULL").fetchone()[0]


def is_allowed(user_id: int) -> bool:
    with connect() as connection:
        return connection.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None


def allow_user(user_id: int) -> None:
    with connect() as connection:
        connection.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (user_id,))


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(GET_EMAIL_TEXT)]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if await asyncio.to_thread(is_allowed, user_id):
        await update.message.reply_text("Нажмите кнопку, чтобы получить почту.", reply_markup=keyboard())
    else:
        await update.message.reply_text("Введите пароль для доступа к боту.", reply_markup=None)


async def give_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await asyncio.to_thread(is_allowed, update.effective_user.id):
        await update.message.reply_text("Сначала введите пароль или отправьте /start.")
        return
    email = await asyncio.to_thread(issue_email, update.effective_user.id)
    if email is None:
        await update.message.reply_text("Свободные почты закончились. Попробуйте позже.")
        return
    await update.message.reply_text(f"Ваши данные:\n<code>{email}</code>", parse_mode="HTML")


async def upload_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Загружать списки может только администратор.")
        return

    document = update.message.document
    if document is None or document.file_size and document.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("Пришлите текстовый файл размером до 10 МБ.")
        return

    file = await document.get_file()
    content = (await file.download_as_bytearray()).decode("utf-8-sig", errors="replace")
    entries = []
    for line in content.splitlines():
        entry = line.strip()
        if not entry:
            continue
        entries.append(entry)

    if not entries:
        await update.message.reply_text("В файле не найдено непустых строк.")
        return

    added, duplicates = await asyncio.to_thread(add_emails, entries)
    await update.message.reply_text(
        f"Готово. Добавлено: {added}. Уже были в базе: {duplicates}.\n"
        f"Свободно сейчас: {await asyncio.to_thread(remaining_count)}."
    )


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(f"Свободных почт: {await asyncio.to_thread(remaining_count)}.")


async def check_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Authorize a user after they send the shared access password."""
    if await asyncio.to_thread(is_allowed, update.effective_user.id):
        return
    if hmac.compare_digest(update.message.text, ACCESS_PASSWORD):
        await asyncio.to_thread(allow_user, update.effective_user.id)
        await update.message.reply_text("Доступ разрешён. Нажмите кнопку, чтобы получить почту.", reply_markup=keyboard())
    else:
        await update.message.reply_text("Неверный пароль. Попробуйте ещё раз.")


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в переменных окружения.")
    if not ADMIN_IDS:
        raise RuntimeError("Укажите хотя бы один ADMIN_IDS в переменных окружения.")

    init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(MessageHandler(filters.Document.ALL, upload_list))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{re.escape(GET_EMAIL_TEXT)}$"), give_email))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_password))
    app.run_polling(allowed_updates=Update.ALL_TYPES) 


if __name__ == "__main__":
    main()
