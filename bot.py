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

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly"
]

GOOGLE_TOKEN_FILE = BASE_DIR / "token.json"
GOOGLE_CREDENTIALS_FILE = BASE_DIR / "credentials.json"

# Set LIBRARY_CACHE_DB to a persistent disk path on your host if
# the platform does not preserve files in the application directory.
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


# ============================================================
# GOOGLE DRIVE CONNECTION
# ============================================================

drive_service = None
drive_lock = threading.Lock()

folder_cache = {}
cache_lock = threading.Lock()


def get_drive_service():
    """
    Create and cache the Google Drive API service.

    Authentication priority:
    1. GOOGLE_TOKEN_JSON_BASE64 environment variable.
    2. Local token.json.
    3. Local credentials.json with interactive OAuth authentication.

    On FadeHost, GOOGLE_TOKEN_JSON_BASE64 should contain the
    Base64-encoded contents of a valid Google OAuth token.json.
    """
    global drive_service

    if drive_service is not None:
        return drive_service

    with drive_lock:
        if drive_service is not None:
            return drive_service

        credentials = None
        encoded_token = os.getenv("GOOGLE_TOKEN_JSON_BASE64", "").strip()

        # Option 1: token supplied through the hosting environment.
        if encoded_token:
            try:
                decoded_token = base64.b64decode(
                    encoded_token,
                    validate=True,
                )
                token_info = json.loads(decoded_token.decode("utf-8"))
                credentials = Credentials.from_authorized_user_info(
                    token_info,
                    GOOGLE_SCOPES,
                )
                logger.info("Google OAuth token loaded from environment.")
            except (
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                KeyError,
            ) as exc:
                raise RuntimeError(
                    "GOOGLE_TOKEN_JSON_BASE64 is invalid. Check that "
                    "FadeHost contains the complete Base64-encoded "
                    "contents of token.json."
                ) from exc

        # Option 2: local token file.
        elif GOOGLE_TOKEN_FILE.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(GOOGLE_TOKEN_FILE),
                    GOOGLE_SCOPES,
                )
                logger.info("Google OAuth token loaded from local token.json.")
            except (
                ValueError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as exc:
                raise RuntimeError(
                    "The local token.json file is invalid. "
                    "Generate a new Google OAuth token."
                ) from exc

        # Refresh expired credentials where possible.
        if credentials and credentials.expired:
            if credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                    logger.info("Google OAuth credentials refreshed.")
                except Exception as exc:
                    raise RuntimeError(
                        "Google OAuth token refresh failed. The token may "
                        "have expired or been revoked. Reauthorize Google "
                        "Drive access and provide an updated token."
                    ) from exc
            else:
                credentials = None
                logger.warning(
                    "Google OAuth credentials expired without a refresh token."
                )

        # Option 3: local interactive OAuth, only if a credentials file exists.
        if not credentials or not credentials.valid:
            if not GOOGLE_CREDENTIALS_FILE.exists():
                raise RuntimeError(
                    "Google Drive authentication is unavailable. Set "
                    "GOOGLE_TOKEN_JSON_BASE64 in the hosting environment "
                    "using a valid token.json, or provide credentials.json "
                    "for local OAuth setup."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(GOOGLE_CREDENTIALS_FILE),
                GOOGLE_SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        # Save refreshed/local credentials where the filesystem permits it.
        try:
            GOOGLE_TOKEN_FILE.write_text(
                credentials.to_json(),
                encoding="utf-8",
            )
        except OSError:
            logger.warning(
                "Could not save token.json locally; continuing with in-memory credentials."
            )

        drive_service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
        logger.info("Google Drive API service initialized successfully.")
        return drive_service


def get_folder_metadata(folder_id):
    service = get_drive_service()
    return (
        service.files()
        .get(
            fileId=folder_id,
            fields="id,name,mimeType,trashed",
            supportsAllDrives=True,
        )
        .execute()
    )


# ============================================================
# PERSISTENT TELEGRAM FILE CACHE
# ============================================================

def init_file_cache():
    """Create the SQLite cache table if it does not already exist."""
    CACHE_DB_FILE.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(CACHE_DB_FILE, timeout=20) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_file_cache (
                drive_file_id TEXT PRIMARY KEY,
                telegram_file_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_size INTEGER,
                cached_at REAL NOT NULL
            )
            """
        )
        connection.commit()

    logger.info("Telegram file cache database ready: %s", CACHE_DB_FILE)


def get_cached_file(drive_file_id):
    """Return a cached Telegram file mapping, or None."""
    with sqlite3.connect(CACHE_DB_FILE, timeout=20) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT drive_file_id, telegram_file_id, filename, file_size, cached_at
            FROM telegram_file_cache
            WHERE drive_file_id = ?
            """,
            (drive_file_id,),
        ).fetchone()

    return dict(row) if row else None


def save_cached_file(drive_file_id, telegram_file_id, filename, file_size):
    """Persist a successful Telegram upload for reuse by later requests."""
    with sqlite3.connect(CACHE_DB_FILE, timeout=20) as connection:
        connection.execute(
            """
            INSERT INTO telegram_file_cache
                (drive_file_id, telegram_file_id, filename, file_size, cached_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(drive_file_id) DO UPDATE SET
                telegram_file_id = excluded.telegram_file_id,
                filename = excluded.filename,
                file_size = excluded.file_size,
                cached_at = excluded.cached_at
            """,
            (
                drive_file_id,
                telegram_file_id,
                filename,
                file_size,
                time.time(),
            ),
        )
        connection.commit()


def delete_cached_file(drive_file_id):
    with sqlite3.connect(CACHE_DB_FILE, timeout=20) as connection:
        connection.execute(
            "DELETE FROM telegram_file_cache WHERE drive_file_id = ?",
            (drive_file_id,),
        )
        connection.commit()


# ============================================================
# DIRECTORY BROWSER
# ============================================================

def list_folder_contents(folder_id, force_refresh=False):
    """Retrieve only the immediate contents of one Drive folder."""
    now = time.monotonic()

    if not force_refresh:
        with cache_lock:
            cached = folder_cache.get(folder_id)
            if cached and now - cached["time"] < CACHE_DURATION:
                return [dict(item) for item in cached["items"]]

    service = get_drive_service()
    items = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=1000,
                pageToken=page_token,
                orderBy="folder,name",
                fields=(
                    "nextPageToken,"
                    "files("
                    "id,name,mimeType,size,"
                    "shortcutDetails(targetId,targetMimeType)"
                    ")"
                ),
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    def sort_key(item):
        mime_type = item.get("mimeType")
        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and item.get("shortcutDetails", {}).get("targetMimeType")
                == FOLDER_MIME
            )
        )
        return (0 if is_folder else 1, item.get("name", "").casefold())

    items.sort(key=sort_key)

    with cache_lock:
        folder_cache[folder_id] = {
            "time": time.monotonic(),
            "items": [dict(item) for item in items],
        }

    return items


# ============================================================
# USER NAVIGATION
# ============================================================

# Navigation history is held separately for each (chat, user) pair.
user_paths = {}
user_paths_lock = threading.Lock()


def get_user_key(update):
    return (update.effective_chat.id, update.effective_user.id)


def get_user_path(update):
    key = get_user_key(update)
    with user_paths_lock:
        if key not in user_paths:
            user_paths[key] = [(ROOT_FOLDER_ID, "AAES DIGITAL LIBRARY")]
        return list(user_paths[key])


def set_user_path(update, path):
    key = get_user_key(update)
    with user_paths_lock:
        user_paths[key] = list(path)


# ============================================================
# KEYBOARDS
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


def navigation_buttons(can_go_back):
    buttons = []

    if can_go_back:
        buttons.append(
            InlineKeyboardButton("⬅️ Back", callback_data="nav:back")
        )

    buttons.extend([
        InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home"),
        InlineKeyboardButton("🔄 Refresh", callback_data="nav:refresh"),
    ])
    return buttons


def make_folder_keyboard(items, page, can_go_back):
    start = page * ITEMS_PER_PAGE
    visible_items = items[start:start + ITEMS_PER_PAGE]
    keyboard = []

    for item in visible_items:
        item_id = item["id"]
        name = item.get("name", "Unnamed document")
        mime_type = item.get("mimeType")
        shortcut_details = item.get("shortcutDetails", {})

        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut_details.get("targetMimeType") == FOLDER_MIME
            )
        )

        label = ("📁 " if is_folder else "📄 ") + name
        if len(label) > 55:
            label = label[:54] + "…"

        prefix = "folder" if is_folder else "file"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"{prefix}:{item_id}")
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

    keyboard.append(navigation_buttons(can_go_back))
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# DIRECTORY SCREEN
# ============================================================

def create_folder_message(path, items):
    current_name = path[-1][1]
    breadcrumb = " › ".join(name for _, name in path)
    folders = 0
    files = 0

    for item in items:
        mime_type = item.get("mimeType")
        shortcut = item.get("shortcutDetails", {})
        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut.get("targetMimeType") == FOLDER_MIME
            )
        )

        if is_folder:
            folders += 1
        else:
            files += 1

    message = (
        f"📚 <b>{html.escape(current_name)}</b>\n\n"
        f"📍 {html.escape(breadcrumb)}\n\n"
        f"📁 Folders: {folders}\n"
        f"📄 Files: {files}\n\n"
        "Select a folder to open it, or select a document to download it."
    )

    if not items:
        message += "\n\nThis folder is empty or contains no accessible items."

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
            (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE,
        )
        page = max(0, min(page, total_pages - 1))

        message = create_folder_message(path, items)
        keyboard = make_folder_keyboard(items, page, len(path) > 1)

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
        logger.exception("Google Drive directory request failed")
        error_message = (
            "❌ I couldn't open this directory.\n\n"
            "Please check Google Drive permissions and try Refresh."
        )

        if edit_message:
            await update.callback_query.edit_message_text(error_message)
        else:
            await update.effective_message.reply_text(error_message)

    except Exception:
        logger.exception("Unexpected directory error")
        error_message = (
            "❌ Something went wrong while opening this directory. "
            "Please try again."
        )

        if edit_message:
            await update.callback_query.edit_message_text(error_message)
        else:
            await update.effective_message.reply_text(error_message)


# ============================================================
# DOWNLOAD FROM GOOGLE DRIVE
# ============================================================

def download_file(file_id):
    service = get_drive_service()

    metadata = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size,shortcutDetails",
            supportsAllDrives=True,
        )
        .execute()
    )

    # Resolve shortcuts before downloading.
    if metadata.get("mimeType") == SHORTCUT_MIME:
        target_id = metadata.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            raise ValueError("This shortcut has no accessible target.")

        metadata = (
            service.files()
            .get(
                fileId=target_id,
                fields="id,name,mimeType,size",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = target_id

    name = metadata.get("name", "document")
    mime_type = metadata.get("mimeType", "application/octet-stream")
    size = metadata.get("size")

    if size is not None:
        try:
            if int(size) > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    "This file exceeds the bot's 45 MB download limit."
                )
        except (TypeError, ValueError) as exc:
            if "45 MB" in str(exc):
                raise

    # Export supported Google Workspace documents to common formats.
    export_mime = None
    extension = ""

    if mime_type == "application/vnd.google-apps.document":
        export_mime = "application/pdf"
        extension = ".pdf"
    elif mime_type == "application/vnd.google-apps.spreadsheet":
        export_mime = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        extension = ".xlsx"
    elif mime_type == "application/vnd.google-apps.presentation":
        export_mime = "application/pdf"
        extension = ".pdf"
    elif mime_type.startswith("application/vnd.google-apps."):
        raise ValueError(
            "This Google Workspace document type is not supported for download."
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
# COMMON POST-DOWNLOAD MENU
# ============================================================

async def show_after_file_menu(context, chat_id):
    """
    Send a fresh menu after the document message.

    This intentionally appears AFTER the file in the chat, so users can
    continue browsing once the document has been delivered.
    """
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
        "Browse your library using buttons. You do not need to type "
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
        "2. Select a folder.\n"
        "3. Continue until you reach the documents.\n"
        "4. Tap a document to download it.\n\n"
        "Use Back to return to the previous folder, Main Menu to start "
        "again, and Refresh to reload a folder.",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# CALLBACK QUERY ACKNOWLEDGEMENT
# ============================================================

async def acknowledge_callback(query):
    """
    Acknowledge a button immediately, before any Drive calls or downloads.
    An expired callback is logged and does not interrupt the action.
    """
    try:
        await query.answer()
    except BadRequest as exc:
        error_text = str(exc).lower()
        if (
            "query is too old" in error_text
            or "query id is invalid" in error_text
            or "response timeout expired" in error_text
        ):
            logger.warning(
                "Callback query expired before acknowledgement; continuing action."
            )
        else:
            logger.warning("Could not acknowledge callback query: %s", exc)
    except TelegramError:
        logger.exception("Telegram failed to acknowledge callback query")


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

    # Crucially, acknowledge once and immediately. Do not call query.answer()
    # again later in this handler, particularly after a long download.
    await acknowledge_callback(query)
    action = query.data or ""

    # Main menu: browse.
    if action == "menu:browse":
        set_user_path(
            update,
            [(ROOT_FOLDER_ID, "AAES DIGITAL LIBRARY")],
        )
        await display_folder(update, context, edit_message=True)
        return

    # Main menu: help.
    if action == "menu:help":
        await query.edit_message_text(
            "ℹ️ <b>HOW TO USE THE LIBRARY</b>\n\n"
            "1. Tap Browse Library.\n"
            "2. Select a folder.\n"
            "3. Continue until you reach the documents.\n"
            "4. Tap a document to download it.\n\n"
            "Use Back to return to the previous folder, Main Menu to start "
            "again, and Refresh to reload a folder.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # Return to the main menu.
    if action == "nav:home":
        set_user_path(
            update,
            [(ROOT_FOLDER_ID, "AAES DIGITAL LIBRARY")],
        )
        await query.edit_message_text(
            "📚 <b>AAES DIGITAL LIBRARY</b>\n\nChoose an option:",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # Navigate one folder upward.
    if action == "nav:back":
        path = get_user_path(update)
        if len(path) > 1:
            path.pop()
        set_user_path(update, path)
        await display_folder(update, context, edit_message=True)
        return

    # Force-refresh current folder.
    if action == "nav:refresh":
        await display_folder(
            update,
            context,
            force_refresh=True,
            edit_message=True,
        )
        return

    # Pagination.
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

    # Open a folder.
    if action.startswith("folder:"):
        selected_id = action.split(":", 1)[1]
        path = get_user_path(update)

        items = await asyncio.to_thread(
            list_folder_contents,
            path[-1][0],
        )

        selected = next(
            (item for item in items if item.get("id") == selected_id),
            None,
        )

        if not selected:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ This folder is no longer available. Please refresh the directory.",
            )
            return

        mime_type = selected.get("mimeType")
        shortcut = selected.get("shortcutDetails", {})
        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut.get("targetMimeType") == FOLDER_MIME
            )
        )

        if not is_folder:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ This item is not a folder.",
            )
            return

        target_id = (
            shortcut.get("targetId")
            if mime_type == SHORTCUT_MIME
            else selected_id
        )

        if not target_id:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ This folder shortcut has no accessible target.",
            )
            return

        path.append((target_id, selected.get("name", "Folder")))
        set_user_path(update, path)
        await display_folder(update, context, edit_message=True)
        return

    # Download a file, using Telegram cache when possible.
    if action.startswith("file:"):
        selected_id = action.split(":", 1)[1]
        path = get_user_path(update)

        items = await asyncio.to_thread(
            list_folder_contents,
            path[-1][0],
        )

        selected = next(
            (item for item in items if item.get("id") == selected_id),
            None,
        )

        if not selected:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ This document is no longer available. Please refresh the directory.",
            )
            return

        mime_type = selected.get("mimeType")
        shortcut = selected.get("shortcutDetails", {})
        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut.get("targetMimeType") == FOLDER_MIME
            )
        )

        if is_folder:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Open this item as a folder instead.",
            )
            return

        chat_id = update.effective_chat.id
        original_name = selected.get("name", "Document")

        status = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Preparing:\n"
                f"<b>{html.escape(original_name)}</b>"
            ),
            parse_mode="HTML",
        )

        # First try the persistent Telegram cache.
        cached = await asyncio.to_thread(get_cached_file, selected_id)

        if cached:
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )

                cached_send_start = time.perf_counter()
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=cached["telegram_file_id"],
                    caption=f"📥 {cached['filename']}",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
                cached_send_elapsed = time.perf_counter() - cached_send_start

                logger.info(
                    "TELEGRAM CACHE HIT | File: %s | Send: %.2fs",
                    cached["filename"],
                    cached_send_elapsed,
                )

                await status.edit_text("✅ Your document has been sent.")
                await show_after_file_menu(context, chat_id)
                return

            except BadRequest:
                # A stale/unusable cached file_id should not permanently
                # prevent the user from accessing the Drive source.
                logger.warning(
                    "Cached Telegram file_id rejected for Drive ID %s; "
                    "removing cache entry and trying Google Drive.",
                    selected_id,
                )
                await asyncio.to_thread(delete_cached_file, selected_id)

            except TelegramError:
                logger.exception("Telegram cache send failed.")
                await status.edit_text(
                    "❌ Telegram could not send the cached document. Please try again."
                )
                return

        try:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.UPLOAD_DOCUMENT,
            )

            logger.info(
                "DOWNLOAD START | File: %s | ID: %s",
                original_name,
                selected_id,
            )

            download_start = time.perf_counter()
            buffer, filename = await asyncio.to_thread(
                download_file,
                selected_id,
            )
            download_elapsed = time.perf_counter() - download_start
            file_size = buffer.getbuffer().nbytes
            file_size_mb = file_size / (1024 * 1024)

            logger.info(
                "DOWNLOAD COMPLETE | File: %s | Size: %.2f MB | Time: %.2fs",
                filename,
                file_size_mb,
                download_elapsed,
            )

            logger.info("TELEGRAM UPLOAD START | File: %s", filename)
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

            upload_elapsed = time.perf_counter() - upload_start
            logger.info(
                "TELEGRAM UPLOAD COMPLETE | File: %s | Time: %.2fs",
                filename,
                upload_elapsed,
            )

            # Save the Telegram file_id only after a successful upload.
            if sent_message.document and sent_message.document.file_id:
                try:
                    await asyncio.to_thread(
                        save_cached_file,
                        selected_id,
                        sent_message.document.file_id,
                        filename,
                        file_size,
                    )
                    logger.info(
                        "TELEGRAM CACHE SAVED | File: %s | Size: %.2f MB",
                        filename,
                        file_size_mb,
                    )
                except Exception:
                    # Cache failure must not turn a successful delivery into
                    # a failed download. The file was already sent to the user.
                    logger.exception("Could not save Telegram cache entry.")

            await status.edit_text("✅ Your document has been sent.")

            logger.info(
                "DOWNLOAD PROCESS COMPLETE | File: %s | Drive: %.2fs | "
                "Telegram: %.2fs | Total: %.2fs",
                filename,
                download_elapsed,
                upload_elapsed,
                download_elapsed + upload_elapsed,
            )

            # Send the main menu AFTER the document so the user can continue.
            await show_after_file_menu(context, chat_id)

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
                "❌ Google Drive could not provide this document. "
                "Check its permissions or try again.",
                reply_markup=main_menu_keyboard(),
            )

        except TelegramError:
            logger.exception("Telegram document upload failed")
            await status.edit_text(
                "❌ Telegram could not send this document. "
                "It may be too large or the upload may have failed.",
                reply_markup=main_menu_keyboard(),
            )

        except Exception:
            logger.exception("Unexpected download error")
            await status.edit_text(
                "❌ An unexpected error occurred while downloading the document.",
                reply_markup=main_menu_keyboard(),
            )

        finally:
            # Release the in-memory download buffer promptly.
            try:
                buffer.close()
            except (UnboundLocalError, AttributeError):
                pass

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
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Unhandled application error without exception details.")


# ============================================================
# START APPLICATION
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Telegram bot token is missing. Set TELEGRAM_BOT_TOKEN in your "
            "environment or local .env file."
        )

    init_file_cache()

    # Verify Drive access before starting Telegram polling.
    root = get_folder_metadata(ROOT_FOLDER_ID)

    if root.get("mimeType") != FOLDER_MIME:
        raise RuntimeError("The configured Google Drive root ID is not a folder.")

    if root.get("trashed"):
        raise RuntimeError("The configured Google Drive root folder is in the trash.")

    logger.info(
        "Connected to Google Drive library: %s",
        root.get("name"),
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    logger.info("AAES DIGITAL LIBRARY is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
