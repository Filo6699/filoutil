"""Command to add a new Moodle session."""

import json
import logging
import re
from urllib.parse import unquote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import (
    create_session_refresh,
    get_active_sessions_for_user,
    get_user_refresh_interval,
)
from filoutil.db.users import get_user_by_telegram_id
from filoutil.moodle_cabinet.notifications_manager import fetch_moodle_user_id
from filoutil.session_refresh.oidc_restore import (
    bootstrap_moodle_session_via_oidc,
    parse_oidc_cookies,
    resolve_sesskey_from_moodle_session,
)

logger = logging.getLogger(__name__)

WIZARD_KEY = "moodle_add_wizard"


def clear_moodle_add_wizard_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(WIZARD_KEY, None)


def start_moodle_add_wizard(
    context: ContextTypes.DEFAULT_TYPE, telegram_id: int, method: str
) -> None:
    state = {"telegram_id": telegram_id, "method": method}
    if method == "oidc":
        state["step"] = "estsauthpersistent"
    context.user_data[WIZARD_KEY] = state


async def _delete_sensitive_messages(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list[int]
) -> None:
    unique_message_ids = list(dict.fromkeys(message_ids))
    for message_id in unique_message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest:
            logger.debug("Could not delete sensitive message %s in chat %s", message_id, chat_id)


def _extract_param(text: str, name: str) -> str | None:
    patterns = [
        rf'"{name}"\s*:\s*"([^"]+)"',
        rf"'{name}'\s*:\s*'([^']+)'",
        rf"\b{name}=([^&\s\"']+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return unquote(match.group(1).strip())
    return None


def _extract_sesskey(text: str) -> str | None:
    patterns = [
        r"M\.cfg\.sesskey[^A-Za-z0-9]*['\"]?([A-Za-z0-9]{6,})",
        r'"sesskey"\s*:\s*"([A-Za-z0-9]{6,})"',
        r"\bsesskey\s*[=:]\s*['\"]?([A-Za-z0-9]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _extract_moodle_session(text: str) -> str | None:
    patterns = [
        r"\bMoodleSession\s*=\s*([A-Za-z0-9]+)",
        r'"MoodleSession"\s*:\s*"([A-Za-z0-9]+)"',
        r"\bMoodleSession\s*[=:]\s*['\"]?([A-Za-z0-9]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    # Accept direct cookie value input (value-only message).
    stripped = text.strip().strip("\"'`")
    if (
        stripped
        and "=" not in stripped
        and len(stripped) > 20
        and re.fullmatch(r"[A-Za-z0-9]+", stripped)
    ):
        return stripped
    return None


def _extract_oidc_data_from_text(text: str) -> dict | None:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            if isinstance(parsed.get("oidc"), dict):
                return parsed["oidc"]
            if "microsoft_cookies" in parsed or "code" in parsed:
                return parsed
    except Exception:
        pass

    cookie_line_match = re.search(r"(?im)^\s*cookie\s*:\s*(.+)$", text)
    cookie_raw = cookie_line_match.group(1).strip() if cookie_line_match else stripped
    cookies = parse_oidc_cookies(cookie_raw)

    code = _extract_param(text, "code")
    state = _extract_param(text, "state")
    session_state = _extract_param(text, "session_state")

    has_ms_cookies = "ESTSAUTHPERSISTENT" in cookies or "ESTSAUTH" in cookies
    if has_ms_cookies:
        data: dict = {"microsoft_cookies": cookies}
        if code and state and session_state:
            data.update({"code": code, "state": state, "session_state": session_state})
        return data

    if code and state and session_state:
        return {"code": code, "state": state, "session_state": session_state}

    return None


def _extract_cookie_value_by_name(text: str, cookie_name: str) -> str | None:
    patterns = [
        rf"\b{cookie_name}\s*=\s*([^;\s\"']+)",
        rf'"{cookie_name}"\s*:\s*"([^"]+)"',
        rf"'{cookie_name}'\s*:\s*'([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    stripped = text.strip().strip("\"'`")
    if stripped and "=" not in stripped and len(stripped) > 20:
        return stripped
    return None


async def _create_session_from_values(
    update: Update,
    sesskey: str,
    moodle_session: str,
    session_name: str | None = None,
    oidc_data: dict | None = None,
) -> bool:
    """Create and validate a Moodle session from parsed values.

    Returns:
        True if the session was successfully created, otherwise False.
    """
    if not update.message or not update.message.from_user:
        return False

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return False

        if not user.moodle_session_agreement or not user.moodle_student_confirmation:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📖 Read & Agree to Terms", callback_data="moodle:add_session"
                        )
                    ],
                    [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
                ]
            )
            await update.message.reply_text(
                "⚠️ Agreement required first.",
                reply_markup=keyboard,
            )
            return False

        active_sessions = get_active_sessions_for_user(db, user.id)
        max_sessions = 5
        if len(active_sessions) >= max_sessions:
            await update.message.reply_text(
                f"❌ You already have {len(active_sessions)} active sessions (maximum: {max_sessions})."
            )
            return False

        valid, moodle_user_id, error_msg = await fetch_moodle_user_id(sesskey, moodle_session)
        if not valid:
            await update.message.reply_text(
                f"❌ Could not validate session credentials: {error_msg or 'Unknown error'}"
            )
            return False

        refresh_interval = get_user_refresh_interval(db, user.id)
        session_refresh = create_session_refresh(
            db,
            user.id,
            sesskey,
            moodle_session,
            refresh_interval,
            name=session_name,
            oidc_data=oidc_data,
        )
        if moodle_user_id:
            session_refresh.moodle_user_id = moodle_user_id
            db.commit()
            db.refresh(session_refresh)

        display_name = session_name or f"Session {session_refresh.id}"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 View Sessions", callback_data="moodle:sessions")],
                [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
            ]
        )
        await update.message.reply_text(
            f"✅ *Moodle Session Added!*\n\n"
            f"*Name:* {display_name}\n"
            f"*Session ID:* {session_refresh.id}\n\n"
            f"{'✅ OIDC auto-recovery enabled.\n\n' if oidc_data else ''}"
            f"You now have {len(active_sessions) + 1} active session(s).",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    return True


async def moodle_add_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /moodle_add command with JSON input."""
    if not await require_module_permission(update, context, "moodle"):
        return
    if not update.message or not update.message.text:
        return

    message_text = update.message.text.strip()
    json_part = (
        message_text[len("/moodle_add") :].strip()
        if message_text.startswith("/moodle_add")
        else message_text
    )
    if not json_part:
        await update.message.reply_text(
            "Use /moodle menu and choose Add Session for step-by-step setup."
        )
        return

    try:
        data = json.loads(json_part)
    except json.JSONDecodeError as e:
        await update.message.reply_text(f"❌ Invalid JSON format: {str(e)}")
        return

    sesskey = data.get("sesskey")
    moodle_session = data.get("moodleSession")
    oidc_data = data.get("oidc") if isinstance(data.get("oidc"), dict) else None
    session_name = data.get("name")

    if (not sesskey or not moodle_session) and oidc_data:
        success, sesskey, moodle_session, updated_oidc_data, error_msg = (
            await bootstrap_moodle_session_via_oidc(oidc_data)
        )
        if not success or not sesskey or not moodle_session:
            await update.message.reply_text(
                f"❌ Failed to bootstrap Moodle session via OIDC: {error_msg or 'Unknown error'}"
            )
            return
        oidc_data = updated_oidc_data or oidc_data
    elif moodle_session and not sesskey:
        success, resolved_sesskey, error_msg = await resolve_sesskey_from_moodle_session(
            moodle_session
        )
        if not success or not resolved_sesskey:
            await update.message.reply_text(
                f"❌ Could not auto-fetch `sesskey`: {error_msg or 'Unknown error'}"
            )
            return
        sesskey = resolved_sesskey

    if not sesskey or not moodle_session:
        await update.message.reply_text("❌ Missing credentials. Provide `moodleSession`.")
        return

    created = await _create_session_from_values(
        update=update,
        sesskey=sesskey,
        moodle_session=moodle_session,
        session_name=session_name,
        oidc_data=oidc_data,
    )
    if created and update.message:
        await _delete_sensitive_messages(
            context, update.message.chat_id, [update.message.message_id]
        )


async def handle_moodle_add_session_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle step-by-step text input for add-session wizard."""
    if not update.message or not update.message.text or not update.message.from_user:
        return False

    state = context.user_data.get(WIZARD_KEY)
    if not isinstance(state, dict):
        return False

    if state.get("telegram_id") != update.message.from_user.id:
        return False

    text = update.message.text.strip()
    if text.lower() in {"/cancel", "cancel"}:
        clear_moodle_add_wizard_state(context)
        await update.message.reply_text("❌ Cancelled.")
        return True

    method = state.get("method")

    if method == "moodle":
        sesskey = _extract_sesskey(text)
        moodle_session = _extract_moodle_session(text)

        if not moodle_session:
            await update.message.reply_text(
                "❌ Could not extract `MoodleSession`.\n"
                "Send fresh Moodle raw cookie text containing `MoodleSession`."
            )
            return True

        if not sesskey:
            success, resolved_sesskey, error_msg = await resolve_sesskey_from_moodle_session(
                moodle_session
            )
            if success and resolved_sesskey:
                sesskey = resolved_sesskey
            else:
                await update.message.reply_text(
                    f"❌ Could not auto-fetch `sesskey`: {error_msg or 'Unknown error'}"
                )
                return True

        created = await _create_session_from_values(update, sesskey, moodle_session)
        if created:
            clear_moodle_add_wizard_state(context)
            await _delete_sensitive_messages(
                context, update.message.chat_id, [update.message.message_id]
            )
        return True

    if method == "oidc":
        step = state.get("step", "estsauthpersistent")

        # If user pasted everything at once, keep this fast path.
        oidc_data = _extract_oidc_data_from_text(text)
        if oidc_data and oidc_data.get("microsoft_cookies"):
            success, sesskey, moodle_session, updated_oidc_data, error_msg = (
                await bootstrap_moodle_session_via_oidc(oidc_data)
            )
            if not success or not sesskey or not moodle_session:
                await update.message.reply_text(
                    f"❌ OIDC validation failed: {error_msg or 'Unknown error'}"
                )
                return True

            created = await _create_session_from_values(
                update=update,
                sesskey=sesskey,
                moodle_session=moodle_session,
                oidc_data=updated_oidc_data or oidc_data,
            )
            if created:
                clear_moodle_add_wizard_state(context)
                await _delete_sensitive_messages(
                    context, update.message.chat_id, [update.message.message_id]
                )
            return True

        if step == "estsauthpersistent":
            value = _extract_cookie_value_by_name(text, "ESTSAUTHPERSISTENT")
            if not value or len(value) < 20:
                await update.message.reply_text(
                    "❌ Could not read `ESTSAUTHPERSISTENT`.\n"
                    "Send either full `ESTSAUTHPERSISTENT=...` or only its value."
                )
                return True

            state["ESTSAUTHPERSISTENT"] = value
            state.setdefault("sensitive_message_ids", []).append(update.message.message_id)
            state["step"] = "estsauth"
            context.user_data[WIZARD_KEY] = state
            await update.message.reply_text(
                "✅ *Step 1/2 saved*\n\n"
                "Now send `ESTSAUTH` value.\n"
                "You can send `ESTSAUTH=...` or only value.",
                parse_mode="Markdown",
            )
            return True

        if step == "estsauth":
            value = _extract_cookie_value_by_name(text, "ESTSAUTH")
            if not value or len(value) < 20:
                await update.message.reply_text(
                    "❌ Could not read `ESTSAUTH`.\n"
                    "Send either full `ESTSAUTH=...` or only its value."
                )
                return True

            oidc_data = {
                "microsoft_cookies": {
                    "ESTSAUTHPERSISTENT": state.get("ESTSAUTHPERSISTENT", ""),
                    "ESTSAUTH": value,
                }
            }
            success, sesskey, moodle_session, updated_oidc_data, error_msg = (
                await bootstrap_moodle_session_via_oidc(oidc_data)
            )
            if not success or not sesskey or not moodle_session:
                await update.message.reply_text(
                    f"❌ OIDC validation failed: {error_msg or 'Unknown error'}\n\n"
                    "Send fresh cookie values and try again."
                )
                return True

            sensitive_message_ids = list(state.get("sensitive_message_ids", []))
            sensitive_message_ids.append(update.message.message_id)
            created = await _create_session_from_values(
                update=update,
                sesskey=sesskey,
                moodle_session=moodle_session,
                oidc_data=updated_oidc_data or oidc_data,
            )
            if created:
                clear_moodle_add_wizard_state(context)
                await _delete_sensitive_messages(
                    context, update.message.chat_id, sensitive_message_ids
                )
            return True

        # Unknown state fallback
        clear_moodle_add_wizard_state(context)
        await update.message.reply_text(
            "❌ Wizard state reset. Please start OIDC flow again from Add Session."
        )
        return True

    return False
