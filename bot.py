import asyncio
import base64
import html
import io
import json
import logging
import os
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

        # ----------------------------------------------------
        # OPTION 1: LOAD TOKEN FROM DEPLOYMENT ENVIRONMENT
        # ----------------------------------------------------

        encoded_token = os.getenv(
            "GOOGLE_TOKEN_JSON_BASE64",
            "",
        ).strip()

        if encoded_token:

            try:
                decoded_token = base64.b64decode(
                    encoded_token,
                    validate=True,
                )

                token_info = json.loads(
                    decoded_token.decode("utf-8")
                )

                credentials = Credentials.from_authorized_user_info(
                    token_info,
                    GOOGLE_SCOPES,
                )

                logger.info(
                    "Google OAuth token loaded from environment."
                )

            except (
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                KeyError,
            ) as exc:

                raise RuntimeError(
                    "GOOGLE_TOKEN_JSON_BASE64 is invalid. "
                    "Check that FadeHost contains the complete "
                    "Base64-encoded contents of token.json."
                ) from exc

        # ----------------------------------------------------
        # OPTION 2: LOAD LOCAL TOKEN.JSON
        # ----------------------------------------------------

        elif GOOGLE_TOKEN_FILE.exists():

            try:
                credentials = Credentials.from_authorized_user_file(
                    str(GOOGLE_TOKEN_FILE),
                    GOOGLE_SCOPES,
                )

                logger.info(
                    "Google OAuth token loaded from local token.json."
                )

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

        # ----------------------------------------------------
        # REFRESH EXPIRED CREDENTIALS
        # ----------------------------------------------------

        if credentials and credentials.expired:

            if credentials.refresh_token:

                try:
                    credentials.refresh(Request())

                    logger.info(
                        "Google OAuth credentials refreshed."
                    )

                except Exception as exc:

                    raise RuntimeError(
                        "Google OAuth token refresh failed. "
                        "The token may have expired or been revoked. "
                        "Reauthorize Google Drive access and provide "
                        "an updated token."
                    ) from exc

            else:

                credentials = None

                logger.warning(
                    "Google OAuth credentials expired without "
                    "a refresh token."
                )

        # ----------------------------------------------------
        # VERIFY CREDENTIALS
        # ----------------------------------------------------

        if not credentials or not credentials.valid:

            # On a deployment server, interactive authentication
            # is normally unavailable. Never attempt to open a
            # browser automatically unless a local credentials
            # file is present.

            if not GOOGLE_CREDENTIALS_FILE.exists():

                raise RuntimeError(
                    "Google Drive authentication is unavailable. "
                    "Set GOOGLE_TOKEN_JSON_BASE64 in the hosting "
                    "environment using a valid token.json, or "
                    "provide credentials.json for local OAuth setup."
                )

            # ------------------------------------------------
            # OPTION 3: LOCAL INTERACTIVE OAUTH
            # ------------------------------------------------

            flow = InstalledAppFlow.from_client_secrets_file(
                str(GOOGLE_CREDENTIALS_FILE),
                GOOGLE_SCOPES,
            )

            credentials = flow.run_local_server(
                port=0
            )

        # ----------------------------------------------------
        # SAVE TOKEN LOCALLY
        # ----------------------------------------------------

        GOOGLE_TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

        # ----------------------------------------------------
        # BUILD GOOGLE DRIVE SERVICE
        # ----------------------------------------------------

        drive_service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        logger.info(
            "Google Drive API service initialized successfully."
        )

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
# DIRECTORY BROWSER
# ============================================================

def list_folder_contents(folder_id, force_refresh=False):
    """
    Retrieve only the immediate contents of one Drive folder.

    The entire library is not recursively indexed.
    """

    now = time.monotonic()

    if not force_refresh:

        with cache_lock:

            cached = folder_cache.get(folder_id)

            if cached and now - cached["time"] < CACHE_DURATION:
                return [
                    dict(item)
                    for item in cached["items"]
                ]

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
                    "id,"
                    "name,"
                    "mimeType,"
                    "size,"
                    "shortcutDetails(targetId,targetMimeType)"
                    ")"
                ),
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        items.extend(
            response.get("files", [])
        )

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    # --------------------------------------------------------
    # SORT FOLDERS BEFORE DOCUMENTS
    # --------------------------------------------------------

    def sort_key(item):

        mime_type = item.get("mimeType")

        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and item.get(
                    "shortcutDetails", {}
                ).get("targetMimeType") == FOLDER_MIME
            )
        )

        return (
            0 if is_folder else 1,
            item.get("name", "").casefold(),
        )

    items.sort(
        key=sort_key
    )

    with cache_lock:

        folder_cache[folder_id] = {
            "time": time.monotonic(),
            "items": [
                dict(item)
                for item in items
            ],
        }

    return items


# ============================================================
# USER NAVIGATION
# ============================================================

# Each user's navigation history is stored separately.
#
# Key:
#     (chat_id, user_id)
#
# Value:
#     [(folder_id, folder_name), ...]

user_paths = {}
user_paths_lock = threading.Lock()


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
                (
                    ROOT_FOLDER_ID,
                    "AAES DIGITAL LIBRARY",
                )
            ]

        return list(
            user_paths[key]
        )


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
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="nav:back",
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
    end = start + ITEMS_PER_PAGE

    visible_items = items[start:end]

    keyboard = []

    for item in visible_items:

        item_id = item["id"]

        name = item.get(
            "name",
            "Unnamed document",
        )

        mime_type = item.get("mimeType")

        shortcut_details = item.get(
            "shortcutDetails",
            {},
        )

        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut_details.get(
                    "targetMimeType"
                ) == FOLDER_MIME
            )
        )

        label = (
            "📁 " if is_folder else "📄 "
        ) + name

        if len(label) > 55:
            label = label[:54] + "…"

        prefix = (
            "folder" if is_folder else "file"
        )

        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=f"{prefix}:{item_id}",
            )
        ])

    total_pages = max(
        1,
        (
            len(items) + ITEMS_PER_PAGE - 1
        ) // ITEMS_PER_PAGE,
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

    folders = 0
    files = 0

    for item in items:

        mime_type = item.get("mimeType")

        shortcut = item.get(
            "shortcutDetails",
            {},
        )

        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut.get(
                    "targetMimeType"
                ) == FOLDER_MIME
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
        "Select a folder to open it, or select a document "
        "to download it."
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
            (
                len(items) + ITEMS_PER_PAGE - 1
            ) // ITEMS_PER_PAGE,
        )

        page = max(
            0,
            min(page, total_pages - 1),
        )

        message = create_folder_message(
            path,
            items,
        )

        keyboard = make_folder_keyboard(
            items,
            page,
            len(path) > 1,
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

        logger.exception(
            "Google Drive directory request failed"
        )

        error_message = (
            "❌ I couldn't open this directory.\n\n"
            "Please check Google Drive permissions and try Refresh."
        )

        if edit_message:

            await update.callback_query.edit_message_text(
                error_message
            )

        else:

            await update.effective_message.reply_text(
                error_message
            )

    except Exception:

        logger.exception(
            "Unexpected directory error"
        )

        error_message = (
            "❌ Something went wrong while opening this directory. "
            "Please try again."
        )

        if edit_message:

            await update.callback_query.edit_message_text(
                error_message
            )

        else:

            await update.effective_message.reply_text(
                error_message
            )


# ============================================================
# DOWNLOADS
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

    # --------------------------------------------------------
    # RESOLVE GOOGLE DRIVE SHORTCUTS
    # --------------------------------------------------------

    if metadata.get("mimeType") == SHORTCUT_MIME:

        target_id = metadata.get(
            "shortcutDetails",
            {},
        ).get("targetId")

        if not target_id:

            raise ValueError(
                "This shortcut has no accessible target."
            )

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

    name = metadata.get(
        "name",
        "document",
    )

    mime_type = metadata.get(
        "mimeType",
        "application/octet-stream",
    )

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

    # --------------------------------------------------------
    # GOOGLE WORKSPACE EXPORT HANDLING
    # --------------------------------------------------------

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

    elif mime_type.startswith(
        "application/vnd.google-apps."
    ):

        raise ValueError(
            "This Google Workspace document type "
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

    # --------------------------------------------------------
    # DOWNLOAD FILE IN CHUNKS
    # --------------------------------------------------------

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
# TELEGRAM COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    set_user_path(
        update,
        [
            (
                ROOT_FOLDER_ID,
                "AAES DIGITAL LIBRARY",
            )
        ],
    )

    await update.effective_message.reply_text(
        "📚 <b>WELCOME TO THE AAES DIGITAL LIBRARY!</b>\n\n"
        "Browse your library using buttons. You do not need to "
        "type book titles, course codes, or keywords.\n\n"
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
        "Use Back to return to the previous folder, "
        "Main Menu to start again, and Refresh to reload a folder.",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    action = query.data or ""

    # --------------------------------------------------------
    # MAIN MENU
    # --------------------------------------------------------

    if action == "menu:browse":

        await query.answer()

        set_user_path(
            update,
            [
                (
                    ROOT_FOLDER_ID,
                    "AAES DIGITAL LIBRARY",
                )
            ],
        )

        await display_folder(
            update,
            context,
            edit_message=True,
        )

        return

    if action == "menu:help":

        await query.answer()

        await query.edit_message_text(
            "ℹ️ <b>HOW TO USE THE LIBRARY</b>\n\n"
            "1. Tap Browse Library.\n"
            "2. Select a folder.\n"
            "3. Continue until you reach the documents.\n"
            "4. Tap a document to download it.\n\n"
            "Use Back to return to the previous folder, "
            "Main Menu to start again, and Refresh to reload a folder.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if action == "nav:home":

        await query.answer()

        set_user_path(
            update,
            [
                (
                    ROOT_FOLDER_ID,
                    "AAES DIGITAL LIBRARY",
                )
            ],
        )

        await query.edit_message_text(
            "📚 <b>AAES DIGITAL LIBRARY</b>\n\n"
            "Choose an option:",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # BACK
    # --------------------------------------------------------

    if action == "nav:back":

        await query.answer()

        path = get_user_path(update)

        if len(path) > 1:
            path.pop()

        set_user_path(
            update,
            path,
        )

        await display_folder(
            update,
            context,
            edit_message=True,
        )

        return

    # --------------------------------------------------------
    # REFRESH
    # --------------------------------------------------------

    if action == "nav:refresh":

        await query.answer(
            "Refreshing directory..."
        )

        await display_folder(
            update,
            context,
            force_refresh=True,
            edit_message=True,
        )

        return

    # --------------------------------------------------------
    # PAGINATION
    # --------------------------------------------------------

    if action == "page:current":

        await query.answer()
        return

    if action.startswith("page:"):

        await query.answer()

        try:

            page = int(
                action.split(":", 1)[1]
            )

        except ValueError:
            return

        await display_folder(
            update,
            context,
            page=page,
            edit_message=True,
        )

        return

    # --------------------------------------------------------
    # OPEN FOLDER
    # --------------------------------------------------------

    if action.startswith("folder:"):

        selected_id = action.split(
            ":",
            1,
        )[1]

        path = get_user_path(update)

        items = await asyncio.to_thread(
            list_folder_contents,
            path[-1][0],
        )

        selected = next(
            (
                item for item in items
                if item.get("id") == selected_id
            ),
            None,
        )

        if not selected:

            await query.answer(
                "This folder is no longer available.",
                show_alert=True,
            )

            return

        mime_type = selected.get("mimeType")

        shortcut = selected.get(
            "shortcutDetails",
            {},
        )

        is_folder = (
            mime_type == FOLDER_MIME
            or (
                mime_type == SHORTCUT_MIME
                and shortcut.get(
                    "targetMimeType"
                ) == FOLDER_MIME
            )
        )

        if not is_folder:

            await query.answer(
                "This item is not a folder.",
                show_alert=True,
            )

            return

        target_id = (
            shortcut.get("targetId")
            if mime_type == SHORTCUT_MIME
            else selected_id
        )

        if not target_id:

            await query.answer(
                "The folder shortcut has no accessible target.",
                show_alert=True,
            )

            return

        await query.answer()

        path.append(
            (
                target_id,
                selected.get(
                    "name",
                    "Folder",
                ),
            )
        )

        set_user_path(
            update,
            path,
        )

        await display_folder(
            update,
            context,
            edit_message=True,
        )

        return

    # --------------------------------------------------------
    # DOWNLOAD FILE
    # --------------------------------------------------------

    if action.startswith("file:"):

        selected_id = action.split(
            ":",
            1,
        )[1]

        path = get_user_path(update)

        items = await asyncio.to_thread(
            list_folder_contents,
            path[-1][0],
        )

        selected = next(
            (
                item for item in items
                if item.get("id") == selected_id
            ),
            None,
        )

        if not selected:

            await query.answer(
                "This document is no longer available.",
                show_alert=True,
            )

            return

        mime_type = selected.get("mimeType")

        shortcut = selected.get(
            "shortcutDetails",
            {},
        )

        if mime_type == FOLDER_MIME or (
            mime_type == SHORTCUT_MIME
            and shortcut.get(
                "targetMimeType"
            ) == FOLDER_MIME
        ):

            await query.answer(
                "Open this item as a folder instead.",
                show_alert=True,
            )

            return

        await query.answer(
            "Preparing your document..."
        )

        status = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "⏳ Preparing:\n"
                f"<b>{html.escape(selected.get('name', 'Document'))}</b>"
            ),
            parse_mode="HTML",
        )

        try:

            # Stage 1: Notify Telegram.

            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.UPLOAD_DOCUMENT,
            )

            # Stage 2: Download from Google Drive.

            logger.info(
                "DOWNLOAD START | File: %s | ID: %s",
                selected.get(
                    "name",
                    "Document",
                ),
                selected_id,
            )

            download_start = time.perf_counter()

            buffer, filename = await asyncio.to_thread(
                download_file,
                selected_id,
            )

            download_elapsed = (
                time.perf_counter() - download_start
            )

            file_size_mb = (
                buffer.getbuffer().nbytes
                / (1024 * 1024)
            )

            logger.info(
                "DOWNLOAD COMPLETE | File: %s | Size: %.2f MB | "
                "Time: %.2f seconds",
                filename,
                file_size_mb,
                download_elapsed,
            )

            # Stage 3: Upload to Telegram.

            logger.info(
                "TELEGRAM UPLOAD START | File: %s",
                filename,
            )

            upload_start = time.perf_counter()

            await context.bot.send_document(
                chat_id=update.effective_chat.id,
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
                "TELEGRAM UPLOAD COMPLETE | File: %s | Time: %.2f seconds",
                filename,
                upload_elapsed,
            )

            await status.edit_text(
                "✅ Your document has been sent."
            )

            logger.info(
                "DOWNLOAD PROCESS COMPLETE | File: %s | "
                "Drive: %.2fs | Telegram: %.2fs | Total: %.2fs",
                filename,
                download_elapsed,
                upload_elapsed,
                download_elapsed + upload_elapsed,
            )

        except ValueError as exc:

            logger.warning(
                "Download rejected: %s",
                exc,
            )

            await status.edit_text(
                f"⚠️ {html.escape(str(exc))}",
                parse_mode="HTML",
            )

        except HttpError:

            logger.exception(
                "Google Drive download failed"
            )

            await status.edit_text(
                "❌ Google Drive could not provide this document. "
                "Check its permissions or try again."
            )

        except TelegramError:

            logger.exception(
                "Telegram document upload failed"
            )

            await status.edit_text(
                "❌ Telegram could not send this document. "
                "It may be too large or the upload may have failed."
            )

        except Exception:

            logger.exception(
                "Unexpected download error"
            )

            await status.edit_text(
                "❌ An unexpected error occurred while downloading "
                "the document."
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Unhandled application error",
        exc_info=(
            (
                type(context.error),
                context.error,
                context.error.__traceback__,
            )
            if context.error
            else None
        ),
    )


# ============================================================
# START APPLICATION
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Telegram bot token is missing. "
            "Set TELEGRAM_BOT_TOKEN in your environment "
            "or local .env file."
        )

    # Verify Drive access before starting Telegram polling.

    root = get_folder_metadata(
        ROOT_FOLDER_ID
    )

    if root.get("mimeType") != FOLDER_MIME:

        raise RuntimeError(
            "The configured Google Drive root ID is not a folder."
        )

    if root.get("trashed"):

        raise RuntimeError(
            "The configured Google Drive root folder is in the trash."
        )

    logger.info(
        "Connected to Google Drive library: %s",
        root.get("name"),
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "AAES DIGITAL LIBRARY is running."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()