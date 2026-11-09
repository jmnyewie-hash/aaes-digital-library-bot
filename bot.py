
import asyncio
import base64
import html
import io
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

ROOT_FOLDER_ID = "1kipRzamreuK0X43kXT1dzFp77MetA45G"

MARINE_FOLDER_ID = os.getenv(
    "MARINE_FOLDER_ID",
    "1RVn295VhslXln36Ait6ESr0g6gO4Zysh"
).strip()

AUTOMOBILE_FOLDER_ID = os.getenv(
    "AUTOMOBILE_FOLDER_ID",
    "131bbV7xYHr5ttIn76kwskR_okMPXCD-2"
).strip()

PROGRAMMES = {
    "existing": ("AAES DIGITAL LIBRARY", ROOT_FOLDER_ID),
    "marine": ("Marine Engineering", MARINE_FOLDER_ID),
    "automobile": ("Automobile Engineering", AUTOMOBILE_FOLDER_ID),
}

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly"
]

GOOGLE_TOKEN_FILE = BASE_DIR / "token.json"
GOOGLE_CREDENTIALS_FILE = BASE_DIR / "credentials.json"

CACHE_DB_FILE = Path(
    os.getenv(
        "LIBRARY_CACHE_DB",
        str(BASE_DIR / "telegram_file_cache.sqlite3"),
    )
).expanduser()

CACHE_DURATION = 60
ITEMS_PER_PAGE = 8
MAX_DOWNLOAD_BYTES = 45 * 1024 * 1024

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("AAES_LIBRARY")

drive_service = None
drive_lock = threading.Lock()
folder_cache = {}
cache_lock = threading.Lock()

user_paths = {}
user_paths_lock = threading.Lock()


# ============================================================
# GOOGLE DRIVE CONNECTION
# ============================================================

def get_drive_service():
    global drive_service

    if drive_service is not None:
        return drive_service

    with drive_lock:
        if drive_service is not None:
            return drive_service

        credentials = None
        encoded = os.getenv(
            "GOOGLE_TOKEN_JSON_BASE64", ""
        ).strip()

        if encoded:
            try:
                decoded = base64.b64decode(
                    encoded, validate=True
                )
                info = json.loads(decoded.decode("utf-8"))
                credentials = Credentials.from_authorized_user_info(
                    info, GOOGLE_SCOPES
                )
                logger.info("Google OAuth loaded from environment.")
            except Exception as exc:
                raise RuntimeError(
                    "Invalid GOOGLE_TOKEN_JSON_BASE64."
                ) from exc

        elif GOOGLE_TOKEN_FILE.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(GOOGLE_TOKEN_FILE),
                    GOOGLE_SCOPES,
                )
                logger.info("Local Google token loaded.")
            except Exception as exc:
                raise RuntimeError(
                    "Invalid token.json. Reauthorize Google Drive."
                ) from exc

        if credentials and credentials.expired:
            if credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                    logger.info("Google OAuth refreshed.")
                except Exception as exc:
                    raise RuntimeError(
                        "Google token refresh failed. "
                        "Reauthorize Google Drive."
                    ) from exc
            else:
                credentials = None

        if not credentials or not credentials.valid:
            if not GOOGLE_CREDENTIALS_FILE.exists():
                raise RuntimeError(
                    "Google credentials unavailable. "
                    "Provide GOOGLE_TOKEN_JSON_BASE64 or credentials.json."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(GOOGLE_CREDENTIALS_FILE),
                GOOGLE_SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        try:
            GOOGLE_TOKEN_FILE.write_text(
                credentials.to_json(),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Could not persist Google token.")

        drive_service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        logger.info("Google Drive connected.")
        return drive_service


def get_folder_metadata(folder_id):
    return get_drive_service().files().get(
        fileId=folder_id,
        fields="id,name,mimeType,trashed",
        supportsAllDrives=True,
    ).execute()


# ============================================================
# PERSISTENT TELEGRAM FILE CACHE
# ============================================================

def init_file_cache():
    CACHE_DB_FILE.parent.mkdir(
        parents=True, exist_ok=True
    )

    with sqlite3.connect(
        CACHE_DB_FILE, timeout=20
    ) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS telegram_file_cache (
                drive_file_id TEXT PRIMARY KEY,
                telegram_file_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_size INTEGER,
                cached_at REAL NOT NULL
            )
        """)
        connection.commit()

    logger.info("Telegram file cache initialized.")


def get_cached_file(drive_file_id):
    with sqlite3.connect(
        CACHE_DB_FILE, timeout=20
    ) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("""
            SELECT * FROM telegram_file_cache
            WHERE drive_file_id = ?
        """, (drive_file_id,)).fetchone()

    return dict(row) if row else None


def save_cached_file(
    drive_file_id, telegram_file_id, filename, file_size
):
    with sqlite3.connect(
        CACHE_DB_FILE, timeout=20
    ) as connection:
        connection.execute("""
            INSERT INTO telegram_file_cache
            (drive_file_id, telegram_file_id, filename,
             file_size, cached_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(drive_file_id) DO UPDATE SET
                telegram_file_id = excluded.telegram_file_id,
                filename = excluded.filename,
                file_size = excluded.file_size,
                cached_at = excluded.cached_at
        """, (
            drive_file_id,
            telegram_file_id,
            filename,
            file_size,
            time.time(),
        ))
        connection.commit()


def delete_cached_file(drive_file_id):
    with sqlite3.connect(
        CACHE_DB_FILE, timeout=20
    ) as connection:
        connection.execute(
            "DELETE FROM telegram_file_cache "
            "WHERE drive_file_id = ?",
            (drive_file_id,),
        )
        connection.commit()


# ============================================================
# DIRECTORY BROWSER
# ============================================================

def is_folder_item(item):
    mime = item.get("mimeType")
    return (
        mime == FOLDER_MIME
        or (
            mime == SHORTCUT_MIME
            and item.get(
                "shortcutDetails", {}
            ).get("targetMimeType") == FOLDER_MIME
        )
    )


def list_folder_contents(folder_id, force_refresh=False):
    now = time.monotonic()

    if not force_refresh:
        with cache_lock:
            cached = folder_cache.get(folder_id)
            if cached and now - cached["time"] < CACHE_DURATION:
                return [
                    dict(item) for item in cached["items"]
                ]

    service = get_drive_service()
    items = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=1000,
            pageToken=page_token,
            orderBy="folder,name",
            fields=(
                "nextPageToken,"
                "files(id,name,mimeType,size,"
                "shortcutDetails(targetId,targetMimeType))"
            ),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")

        if not page_token:
            break

    items.sort(
        key=lambda item: (
            0 if is_folder_item(item) else 1,
            item.get("name", "").casefold(),
        )
    )

    with cache_lock:
        folder_cache[folder_id] = {
            "time": time.monotonic(),
            "items": [dict(item) for item in items],
        }

    return items


# ============================================================
# USER NAVIGATION
# ============================================================

def get_user_key(update):
    return (
        update.effective_chat.id,
        update.effective_user.id,
    )


def get_user_path(update):
    key = get_user_key(update)

    with user_paths_lock:
        if key not in user_paths:
            user_paths[key] = [
                (ROOT_FOLDER_ID, "AAES DIGITAL LIBRARY")
            ]
        return list(user_paths[key])


def set_user_path(update, path):
    with user_paths_lock:
        user_paths[get_user_key(update)] = list(path)


# ============================================================
# TELEGRAM KEYBOARDS
# ============================================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📂 Browse Library",
                callback_data="menu:browse",
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ How to Use",
                callback_data="menu:help",
            )
        ],
    ])


def programme_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📚 Aerospace Engineering",
                callback_data="programme:existing",
            )
        ],
        [
            InlineKeyboardButton(
                "🚢 Marine Engineering",
                callback_data="programme:marine",
            )
        ],
        [
            InlineKeyboardButton(
                "🚗 Automobile Engineering",
                callback_data="programme:automobile",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Main Menu",
                callback_data="nav:home",
            )
        ],
    ])


def navigation_buttons(can_go_back):
    buttons = []

    if can_go_back:
        buttons.append(
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="nav:back",
            )
        )
    else:
        buttons.append(
            InlineKeyboardButton(
                "📚 Programmes",
                callback_data="nav:programmes",
            )
        )

    buttons.extend([
        InlineKeyboardButton(
            "🏠 Main Menu",
            callback_data="nav:home",
        ),
        InlineKeyboardButton(
            "🔄 Refresh",
            callback_data="nav:refresh",
        ),
    ])

    return buttons


def make_folder_keyboard(items, page, can_go_back):
    start = page * ITEMS_PER_PAGE
    visible = items[start:start + ITEMS_PER_PAGE]
    keyboard = []

    for item in visible:
        item_id = item["id"]
        name = item.get("name", "Unnamed document")
        folder = is_folder_item(item)

        label = ("📁 " if folder else "📄 ") + name

        if len(label) > 55:
            label = label[:54] + "…"

        prefix = "folder" if folder else "file"

        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=f"{prefix}:{item_id}",
            )
        ])

    total_pages = max(
        1,
        (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE,
    )

    if total_pages > 1:
        page_buttons = []

        if page > 0:
            page_buttons.append(
                InlineKeyboardButton(
                    "⬅️ Previous",
                    callback_data=f"page:{page - 1}",
                )
            )

        page_buttons.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}",
                callback_data="page:current",
            )
        )

        if page + 1 < total_pages:
            page_buttons.append(
                InlineKeyboardButton(
                    "Next ➡️",
                    callback_data=f"page:{page + 1}",
                )
            )

        keyboard.append(page_buttons)

    keyboard.append(
        navigation_buttons(can_go_back)
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# DIRECTORY SCREEN
# ============================================================

def create_folder_message(path, items):
    current_name = path[-1][1]
    breadcrumb = " › ".join(
        name for _, name in path
    )

    folders = sum(
        is_folder_item(item) for item in items
    )
    files = len(items) - folders

    message = (
        f"📚 <b>{html.escape(current_name)}</b>\n\n"
        f"📍 {html.escape(breadcrumb)}\n\n"
        f"📁 Folders: {folders}\n"
        f"📄 Files: {files}\n\n"
        "Select a folder to open it, or select "
        "a document to download it."
    )

    if not items:
        message += (
            "\n\nThis folder is empty or contains "
            "no accessible items."
        )

    return message


async def display_folder(
    update,
    context,
    page=0,
    force_refresh=False,
    edit_message=False,
):
    path = get_user_path(update)
    folder_id = path[-1][0]

    try:
        items = await asyncio.to_thread(
            list_folder_contents,
            folder_id,
            force_refresh,
        )

        total_pages = max(
            1,
            (len(items) + ITEMS_PER_PAGE - 1)
            // ITEMS_PER_PAGE,
        )
        page = max(0, min(page, total_pages - 1))

        message = create_folder_message(path, items)
        keyboard = make_folder_keyboard(
            items, page, len(path) > 1
        )

        if edit_message:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await update.effective_message.reply_text(
                message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )

    except HttpError:
        logger.exception("Google Drive directory error")
        error_message = (
            "❌ Couldn't open this directory.\n\n"
            "Check Google Drive permissions and try Refresh."
        )

        if edit_message:
            await update.callback_query.edit_message_text(
                error_message,
                reply_markup=programme_keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                error_message,
                reply_markup=programme_keyboard(),
            )

    except Exception:
        logger.exception("Unexpected directory error")
        error_message = (
            "❌ Something went wrong opening this directory. "
            "Please try again."
        )

        if edit_message:
            await update.callback_query.edit_message_text(
                error_message,
                reply_markup=programme_keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                error_message,
                reply_markup=programme_keyboard(),
            )


# ============================================================
# GOOGLE DRIVE DOWNLOADS
# ============================================================

def download_file(file_id):
    service = get_drive_service()

    metadata = service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size,shortcutDetails",
        supportsAllDrives=True,
    ).execute()

    if metadata.get("mimeType") == SHORTCUT_MIME:
        target_id = metadata.get(
            "shortcutDetails", {}
        ).get("targetId")

        if not target_id:
            raise ValueError(
                "This shortcut has no accessible target."
            )

        metadata = service.files().get(
            fileId=target_id,
            fields="id,name,mimeType,size",
            supportsAllDrives=True,
        ).execute()

        file_id = target_id

    name = metadata.get("name", "document")
    mime_type = metadata.get(
        "mimeType", "application/octet-stream"
    )
    size = metadata.get("size")

    if size is not None and int(size) > MAX_DOWNLOAD_BYTES:
        raise ValueError(
            "This file exceeds the bot's 45 MB download limit."
        )

    export_mime = None
    extension = ""

    if mime_type == "application/vnd.google-apps.document":
        export_mime = "application/pdf"
        extension = ".pdf"

    elif mime_type == "application/vnd.google-apps.spreadsheet":
        export_mime = (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
        extension = ".xlsx"

    elif mime_type == "application/vnd.google-apps.presentation":
        export_mime = "application/pdf"
        extension = ".pdf"

    elif mime_type.startswith("application/vnd.google-apps."):
        raise ValueError(
            "This Google Workspace file type "
            "is not supported for download."
        )

    if export_mime:
        request = service.files().export_media(
            fileId=file_id,
            mimeType=export_mime,
        )
        if not name.lower().endswith(extension):
            name += extension
    else:
        request = service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buffer,
        request,
        chunksize=1024 * 1024,
    )

    done = False

    while not done:
        _, done = downloader.next_chunk()

        if buffer.tell() > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                "This file exceeds the bot's 45 MB download limit."
            )

    buffer.seek(0)
    return buffer, name


# ============================================================
# POST-DOWNLOAD MENU
# ============================================================

async def show_after_file_menu(context, chat_id):
    await context.bot.send_message(
        chat_id=chat_id,
        text="📚 <b>What would you like to do next?</b>",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    set_user_path(
        update,
        [(ROOT_FOLDER_ID, "AAES DIGITAL LIBRARY")],
    )

    await update.effective_message.reply_text(
        "📚 <b>WELCOME TO THE AAES DIGITAL LIBRARY!</b>\n\n"
        "Access course materials for the available "
        "engineering programmes.\n\n"
        "Browse using buttons. You do not need to type "
        "book titles, course codes, or keywords.\n\n"
        "Choose an option below.",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "ℹ️ <b>HOW TO USE THE LIBRARY</b>\n\n"
        "1. Tap Browse Library.\n"
        "2. Select your engineering programme.\n"
        "3. Open the folders containing your materials.\n"
        "4. Tap a document to download it.\n\n"
        "Use Back to return to the previous folder, "
        "Programmes to switch libraries, "
        "Main Menu to start again, "
        "and Refresh to reload a folder.",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# CALLBACK ACKNOWLEDGEMENT
# ============================================================

async def acknowledge_callback(query):
    try:
        await query.answer()
    except BadRequest as exc:
        logger.warning(
            "Callback acknowledgement failed: %s", exc
        )
    except TelegramError:
        logger.exception(
            "Telegram callback acknowledgement error"
        )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query is None:
        return

    await acknowledge_callback(query)
    action = query.data or ""

    # Browse available programmes
    if action in ("menu:browse", "nav:programmes"):
        await query.edit_message_text(
            "📚 <b>SELECT YOUR LIBRARY</b>\n\n"
            "Choose the engineering programme "
            "whose materials you wish to browse.",
            reply_markup=programme_keyboard(),
            parse_mode="HTML",
        )
        return

    # Select a programme
    if action.startswith("programme:"):
        programme_key = action.split(":", 1)[1]

        if programme_key not in PROGRAMMES:
            return

        programme_name, folder_id = PROGRAMMES[
            programme_key
        ]

        if not folder_id:
            await query.edit_message_text(
                "⚠️ This programme has not been configured.",
                reply_markup=programme_keyboard(),
            )
            return

        set_user_path(
            update,
            [(folder_id, programme_name)],
        )

        await display_folder(
            update,
            context,
            edit_message=True,
        )
        return

    # Help
    if action == "menu:help":
        await query.edit_message_text(
            "ℹ️ <b>HOW TO USE THE LIBRARY</b>\n\n"
            "1. Tap Browse Library.\n"
            "2. Select your engineering programme.\n"
            "3. Open the folders containing your materials.\n"
            "4. Tap a document to download it.\n\n"
            "Use Back, Programmes, Main Menu, "
            "or Refresh to navigate.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # Main menu
    if action == "nav:home":
        set_user_path(
            update,
            [(ROOT_FOLDER_ID, "AAES DIGITAL LIBRARY")],
        )

        await query.edit_message_text(
            "📚 <b>AAES DIGITAL LIBRARY</b>\n\n"
            "Choose an option:",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # Back
    if action == "nav:back":
        path = get_user_path(update)

        if len(path) > 1:
            path.pop()
            set_user_path(update, path)

            await display_folder(
                update,
                context,
                edit_message=True,
            )
        else:
            await query.edit_message_text(
                "📚 <b>SELECT YOUR LIBRARY</b>",
                reply_markup=programme_keyboard(),
                parse_mode="HTML",
            )
        return

    # Refresh
    if action == "nav:refresh":
        await display_folder(
            update,
            context,
            force_refresh=True,
            edit_message=True,
        )
        return

    # Pagination
    if action == "page:current":
        return

    if action.startswith("page:"):
        try:
            page = int(action.split(":", 1)[1])
        except ValueError:
            return

        await display_folder(
            update,
            context,
            page=page,
            edit_message=True,
        )
        return

    # Open folder
    if action.startswith("folder:"):
        selected_id = action.split(":", 1)[1]
        path = get_user_path(update)

        try:
            items = await asyncio.to_thread(
                list_folder_contents,
                path[-1][0],
            )
        except Exception:
            logger.exception("Folder lookup failed")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Unable to retrieve folder information.",
            )
            return

        selected = next(
            (
                item for item in items
                if item.get("id") == selected_id
            ),
            None,
        )

        if not selected:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Folder unavailable. Please refresh.",
            )
            return

        if not is_folder_item(selected):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ This item is not a folder.",
            )
            return

        mime_type = selected.get("mimeType")
        shortcut = selected.get("shortcutDetails", {})

        target_id = (
            shortcut.get("targetId")
            if mime_type == SHORTCUT_MIME
            else selected_id
        )

        if not target_id:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Folder shortcut has no accessible target.",
            )
            return

        path.append(
            (target_id, selected.get("name", "Folder"))
        )
        set_user_path(update, path)

        await display_folder(
            update,
            context,
            edit_message=True,
        )
        return

    # Download file
    if action.startswith("file:"):
        selected_id = action.split(":", 1)[1]
        path = get_user_path(update)
        chat_id = update.effective_chat.id

        try:
            items = await asyncio.to_thread(
                list_folder_contents,
                path[-1][0],
            )
        except Exception:
            logger.exception("File lookup failed")
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Could not retrieve file information.",
            )
            return

        selected = next(
            (
                item for item in items
                if item.get("id") == selected_id
            ),
            None,
        )

        if not selected:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Document unavailable. Please refresh.",
            )
            return

        if is_folder_item(selected):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Open this item as a folder instead.",
            )
            return

        original_name = selected.get("name", "Document")

        status = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Preparing:\n"
                f"<b>{html.escape(original_name)}</b>"
            ),
            parse_mode="HTML",
        )

        # Attempt cached Telegram delivery
        try:
            cached = await asyncio.to_thread(
                get_cached_file, selected_id
            )
        except Exception:
            logger.exception("Cache lookup failed")
            cached = None

        if cached:
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )

                await context.bot.send_document(
                    chat_id=chat_id,
                    document=cached["telegram_file_id"],
                    caption=f"📥 {cached['filename']}",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )

                logger.info(
                    "TELEGRAM CACHE HIT | %s",
                    cached["filename"],
                )

                await status.edit_text(
                    "✅ Your document has been sent."
                )
                await show_after_file_menu(
                    context, chat_id
                )
                return

            except BadRequest:
                logger.warning(
                    "Cached Telegram file rejected. "
                    "Retrying from Google Drive."
                )
                await asyncio.to_thread(
                    delete_cached_file, selected_id
                )

            except TelegramError:
                logger.exception(
                    "Telegram cached delivery failed"
                )
                await status.edit_text(
                    "❌ Telegram could not send the "
                    "cached document. Please try again."
                )
                return

        buffer = None

        try:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.UPLOAD_DOCUMENT,
            )

            logger.info(
                "DOWNLOAD START | %s | %s",
                original_name,
                selected_id,
            )

            download_start = time.perf_counter()

            buffer, filename = await asyncio.to_thread(
                download_file, selected_id
            )

            download_elapsed = (
                time.perf_counter() - download_start
            )

            file_size = buffer.getbuffer().nbytes
            file_size_mb = file_size / (1024 * 1024)

            logger.info(
                "DOWNLOAD COMPLETE | %s | %.2f MB | %.2fs",
                filename,
                file_size_mb,
                download_elapsed,
            )

            upload_start = time.perf_counter()

            sent_message = await context.bot.send_document(
                chat_id=chat_id,
                document=buffer,
                filename=filename,
                caption=f"📥 {filename}",
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

            upload_elapsed = (
                time.perf_counter() - upload_start
            )

            logger.info(
                "TELEGRAM UPLOAD COMPLETE | %s | %.2fs",
                filename,
                upload_elapsed,
            )

            if (
                sent_message.document
                and sent_message.document.file_id
            ):
                try:
                    await asyncio.to_thread(
                        save_cached_file,
                        selected_id,
                        sent_message.document.file_id,
                        filename,
                        file_size,
                    )
                except Exception:
                    logger.exception(
                        "Could not save Telegram cache entry"
                    )

            await status.edit_text(
                "✅ Your document has been sent."
            )

            await show_after_file_menu(
                context, chat_id
            )

        except ValueError as exc:
            logger.warning("Download rejected: %s", exc)
            await status.edit_text(
                f"⚠️ {html.escape(str(exc))}",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )

        except HttpError:
            logger.exception("Google Drive download failed")
            await status.edit_text(
                "❌ Google Drive could not provide this "
                "document. Check permissions or try again.",
                reply_markup=main_menu_keyboard(),
            )

        except TelegramError:
            logger.exception("Telegram upload failed")
            await status.edit_text(
                "❌ Telegram could not send this document.",
                reply_markup=main_menu_keyboard(),
            )

        except Exception:
            logger.exception("Unexpected download error")
            await status.edit_text(
                "❌ An unexpected error occurred while "
                "downloading this document.",
                reply_markup=main_menu_keyboard(),
            )

        finally:
            if buffer is not None:
                buffer.close()

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if error:
        logger.error(
            "Unhandled application error",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )
    else:
        logger.error(
            "Unhandled application error "
            "without exception details."
        )


# ============================================================
# START APPLICATION
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Telegram bot token missing. "
            "Set TELEGRAM_BOT_TOKEN in .env "
            "or your hosting environment."
        )

    init_file_cache()

    # Validate each configured Google Drive folder
    for key, (name, folder_id) in PROGRAMMES.items():
        if not folder_id:
            raise RuntimeError(
                f"Folder ID missing for {name}."
            )

        try:
            metadata = get_folder_metadata(folder_id)

            if metadata.get("mimeType") != FOLDER_MIME:
                raise RuntimeError(
                    f"{name} does not point to a Drive folder."
                )

            if metadata.get("trashed"):
                raise RuntimeError(
                    f"{name} folder is in the trash."
                )

            logger.info(
                "LIBRARY AVAILABLE | %s | %s",
                name,
                metadata.get("name"),
            )

        except HttpError as exc:
            raise RuntimeError(
                f"Google Drive folder inaccessible: {name}. "
                "Check sharing permissions."
            ) from exc

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )
    application.add_handler(
        CommandHandler("help", help_command)
    )
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )
    application.add_error_handler(error_handler)

    logger.info(
        "AAES DIGITAL LIBRARY is running "
        "with Marine and Automobile Engineering."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
