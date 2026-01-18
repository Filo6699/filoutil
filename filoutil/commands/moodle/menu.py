"""Moodle menu commands and callbacks."""

import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from filoutil.auth import require_module_permission
from filoutil.db.postgres import SessionLocal
from filoutil.db.session_refresh import get_active_sessions_for_user, get_user_refresh_interval
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)

# Security notice text for Moodle session agreement
MOODLE_SESSION_SECURITY_NOTICE = (
    "⚠️ *Security Notice & Terms*\n\n"
    "Before adding a Moodle session, please read this carefully:\n\n"
    "*What you're sharing:*\n"
    "• `MoodleSession` cookie - your active login session\n"
    "• `sesskey` - session security key\n\n"
    "⚠️ By sharing these credentials with this bot, you are granting "
    "it full access to your Moodle account. The bot will be able to:\n"
    "• View your courses, grades, and notifications\n"
    "• Access any data visible in your Moodle account\n"
    "• Perform actions on your behalf\n\n"
    "Only use this bot if you trust the developers.\n\n"
    "*How to get these values:*\n"
    "1. Open your Moodle site in a browser and log in\n"
    "2. Press F12 to open Developer Tools\n"
    "3. Go to Application/Storage → Cookies\n"
    "4. Find and copy the `MoodleSession` cookie value\n"
    "5. Go to Console tab and run: `M.cfg.sesskey`\n"
    "6. Copy the sesskey value\n\n"
    "*Please confirm both statements below:*"
)

# Russian version of the security notice
MOODLE_SESSION_SECURITY_NOTICE_RU = (
    "⚠️ *Уведомление о безопасности и условия*\n\n"
    "Перед добавлением сессии Moodle, пожалуйста, внимательно прочитайте:\n\n"
    "*Что вы передаёте:*\n"
    "• Cookie `MoodleSession` - ваша активная сессия входа\n"
    "• `sesskey` - ключ безопасности сессии\n\n"
    "⚠️ Передавая эти учётные данные этому боту, вы предоставляете "
    "ему полный доступ к вашему аккаунту Moodle. Бот сможет:\n"
    "• Просматривать ваши курсы, оценки и уведомления\n"
    "• Получать доступ к любым данным, видимым в вашем аккаунте Moodle\n"
    "• Выполнять действия от вашего имени\n\n"
    "Используйте этого бота только если вы доверяете разработчикам.\n\n"
    "*Как получить эти значения:*\n"
    "1. Откройте ваш сайт Moodle в браузере и войдите в систему\n"
    "2. Нажмите F12 для открытия инструментов разработчика\n"
    "3. Перейдите в Application/Storage → Cookies\n"
    "4. Найдите и скопируйте значение cookie `MoodleSession`\n"
    "5. Перейдите во вкладку Console и выполните: `M.cfg.sesskey`\n"
    "6. Скопируйте значение sesskey\n\n"
    "*Пожалуйста, подтвердите оба утверждения ниже:*"
)


def get_security_notice_text(lang: str = "en") -> str:
    """Get security notice text in the specified language.

    Args:
        lang: Language code ('en' or 'ru')

    Returns:
        Security notice text in the requested language
    """
    if lang == "ru":
        return MOODLE_SESSION_SECURITY_NOTICE_RU
    return MOODLE_SESSION_SECURITY_NOTICE


def get_moodle_menu_keyboard(user_role: str = "user") -> InlineKeyboardMarkup:
    """Generate the Moodle menu keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("📋 My Sessions", callback_data="moodle:sessions"),
            InlineKeyboardButton("➕ Add Session", callback_data="moodle:add_session"),
        ],
        [InlineKeyboardButton("🔔 Notifications", callback_data="moodle:notifications")],
        [InlineKeyboardButton("📊 Grades", callback_data="moodle:grades")],
        [
            InlineKeyboardButton("⚙️ Refresh Settings", callback_data="moodle:refresh_settings"),
            InlineKeyboardButton(
                "⚙️ Notification Settings", callback_data="moodle:notification_settings"
            ),
        ],
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(keyboard)


async def moodle_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /moodle command to show the Moodle menu."""
    if not await require_module_permission(update, context, "moodle"):
        return

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, update.message.from_user.id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return

        active_sessions = get_active_sessions_for_user(db, user.id)
        session_count = len(active_sessions)

        text = (
            f"🎓 *Moodle Menu*\n\n"
            f"Manage your Moodle sessions and notifications.\n\n"
            f"*Active Sessions:* {session_count}"
        )

        await update.message.reply_text(
            text, reply_markup=get_moodle_menu_keyboard(user.role), parse_mode="Markdown"
        )


async def show_moodle_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper function to show the Moodle menu."""
    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, query.from_user.id)
        if not user:
            return

        active_sessions = get_active_sessions_for_user(db, user.id)
        session_count = len(active_sessions)

        text = (
            f"🎓 *Moodle Menu*\n\n"
            f"Manage your Moodle sessions and notifications.\n\n"
            f"*Active Sessions:* {session_count}"
        )

        try:
            if query.message.photo:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=get_moodle_menu_keyboard(user.role),
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    text, reply_markup=get_moodle_menu_keyboard(user.role), parse_mode="Markdown"
                )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


def format_session_info(session, index: int = None) -> str:
    """Format session information for display."""
    name = session.name or f"Session {session.id}"
    index_str = f"{index + 1}. " if index is not None else ""

    # Calculate duration
    if session.ended_at:
        duration = session.ended_at - session.started_at
    else:
        duration = datetime.utcnow() - session.started_at

    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        duration_str = f"{hours}h {minutes}m"
    elif minutes > 0:
        duration_str = f"{minutes}m {seconds}s"
    else:
        duration_str = f"{seconds}s"

    status_emoji = "🟢" if session.status == "running" else "🔴"

    # Show first 8 characters of moodleSession
    moodle_session_preview = (
        session.moodleSession[:8] + "..."
        if len(session.moodleSession) > 8
        else session.moodleSession
    )

    return (
        f"{index_str}{status_emoji} *{name}*\n"
        f"   ID: `{session.id}`\n"
        f"   Status: {session.status}\n"
        f"   Duration: {duration_str}\n"
        f"   Refresh: {session.refresh_interval_s // 60}m\n"
        f"   Session: `{moodle_session_preview}`"
    )


def get_sessions_list_keyboard(
    sessions: list, page: int = 0, per_page: int = 5
) -> InlineKeyboardMarkup:
    """Generate keyboard for sessions list."""
    keyboard = []

    # Pagination
    total_pages = (len(sessions) + per_page - 1) // per_page if sessions else 1
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_sessions = sessions[start_idx:end_idx]

    # Session buttons
    for idx, session in enumerate(page_sessions):
        display_idx = start_idx + idx
        name = session.name or f"Session {session.id}"
        status_emoji = "🟢" if session.status == "running" else "🔴"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{status_emoji} {name}", callback_data=f"moodle:session:{session.id}"
                )
            ]
        )

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"moodle:sessions:page:{page - 1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"moodle:sessions:page:{page + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Back button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")])

    return InlineKeyboardMarkup(keyboard)


async def moodle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for the Moodle menu."""
    query = update.callback_query
    if not query:
        return

    # Check permission
    if not await require_module_permission(update, context, "moodle"):
        return

    data = query.data.split(":")
    action = data[1]

    # Answer callback early for most actions, but let stop_session answer itself with a message
    if action != "stop_session":
        await query.answer()

    if action == "menu":
        await show_moodle_menu(query, context)

    elif action == "sessions":
        # Show sessions list
        from filoutil.commands.moodle.sessions import show_sessions_list

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            try:
                page = int(data[2]) if len(data) > 2 and data[2] == "page" else 0
                if len(data) > 3 and data[2] == "page":
                    page = int(data[3])
                if page < 0:
                    raise ValueError("Invalid page number")
            except (ValueError, IndexError):
                await query.answer("❌ Invalid request.", show_alert=True)
                return

            await show_sessions_list(db, user.id, query, context, page=page)

    elif action == "toggle_student":
        # Toggle student confirmation
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            # Toggle the student confirmation
            user.moodle_student_confirmation = not user.moodle_student_confirmation
            db.commit()

            # Get language from callback data (default to 'en')
            lang = data[2] if len(data) > 2 else "en"

            # Refresh the agreement screen with updated checkboxes
            # Rebuild the agreement UI
            text = get_security_notice_text(lang)

            # Build keyboard with updated checkboxes
            terms_agreed = user.moodle_session_agreement
            student_confirmed = user.moodle_student_confirmation

            keyboard_buttons = []

            # Language toggle button
            lang_button_text = "In English" if lang == "ru" else "На русском"
            lang_toggle = "en" if lang == "ru" else "ru"
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        lang_button_text, callback_data=f"moodle:switch_lang:{lang_toggle}"
                    )
                ]
            )

            # Student confirmation button
            student_icon = "☑️" if student_confirmed else "⬜"
            student_text = (
                "Я подтверждаю, что я студент" if lang == "ru" else "I confirm that I'm a student"
            )
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        f"{student_icon} {student_text}",
                        callback_data=f"moodle:toggle_student:{lang}",
                    )
                ]
            )

            # Terms agreement button
            terms_icon = "☑️" if terms_agreed else "⬜"
            terms_text = (
                "Я понимаю и соглашаюсь с условиями выше"
                if lang == "ru"
                else "I understand and agree to the terms above"
            )
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        f"{terms_icon} {terms_text}", callback_data=f"moodle:toggle_terms:{lang}"
                    )
                ]
            )

            # Continue button (only enabled if both are checked)
            if terms_agreed and student_confirmed:
                continue_text = "✅ Продолжить" if lang == "ru" else "✅ Continue"
                keyboard_buttons.append(
                    [InlineKeyboardButton(continue_text, callback_data="moodle:finalize_agreement")]
                )

            cancel_text = "❌ Отмена" if lang == "ru" else "❌ Cancel"
            keyboard_buttons.append(
                [InlineKeyboardButton(cancel_text, callback_data="moodle:menu")]
            )

            keyboard = InlineKeyboardMarkup(keyboard_buttons)

            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "toggle_terms":
        # Toggle terms agreement
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            # Toggle the terms agreement
            user.moodle_session_agreement = not user.moodle_session_agreement
            db.commit()

            # Get language from callback data (default to 'en')
            lang = data[2] if len(data) > 2 else "en"

            # Refresh the agreement screen with updated checkboxes
            # Rebuild the agreement UI
            text = get_security_notice_text(lang)

            # Build keyboard with updated checkboxes
            terms_agreed = user.moodle_session_agreement
            student_confirmed = user.moodle_student_confirmation

            keyboard_buttons = []

            # Language toggle button
            lang_button_text = "In English" if lang == "ru" else "На русском"
            lang_toggle = "en" if lang == "ru" else "ru"
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        lang_button_text, callback_data=f"moodle:switch_lang:{lang_toggle}"
                    )
                ]
            )

            # Student confirmation button
            student_icon = "☑️" if student_confirmed else "⬜"
            student_text = (
                "Я подтверждаю, что я студент" if lang == "ru" else "I confirm that I'm a student"
            )
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        f"{student_icon} {student_text}",
                        callback_data=f"moodle:toggle_student:{lang}",
                    )
                ]
            )

            # Terms agreement button
            terms_icon = "☑️" if terms_agreed else "⬜"
            terms_text = (
                "Я понимаю и соглашаюсь с условиями выше"
                if lang == "ru"
                else "I understand and agree to the terms above"
            )
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        f"{terms_icon} {terms_text}", callback_data=f"moodle:toggle_terms:{lang}"
                    )
                ]
            )

            # Continue button (only enabled if both are checked)
            if terms_agreed and student_confirmed:
                continue_text = "✅ Продолжить" if lang == "ru" else "✅ Continue"
                keyboard_buttons.append(
                    [InlineKeyboardButton(continue_text, callback_data="moodle:finalize_agreement")]
                )

            cancel_text = "❌ Отмена" if lang == "ru" else "❌ Cancel"
            keyboard_buttons.append(
                [InlineKeyboardButton(cancel_text, callback_data="moodle:menu")]
            )

            keyboard = InlineKeyboardMarkup(keyboard_buttons)

            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "switch_lang":
        # Switch language for security notice
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                await query.answer("❌ User not found.", show_alert=True)
                return

            # Get target language from callback data
            lang = data[2] if len(data) > 2 else "en"

            # Build the agreement UI with the new language
            text = get_security_notice_text(lang)

            # Build keyboard with updated checkboxes
            terms_agreed = user.moodle_session_agreement
            student_confirmed = user.moodle_student_confirmation

            keyboard_buttons = []

            # Language toggle button
            lang_button_text = "In English" if lang == "ru" else "На русском"
            lang_toggle = "en" if lang == "ru" else "ru"
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        lang_button_text, callback_data=f"moodle:switch_lang:{lang_toggle}"
                    )
                ]
            )

            # Student confirmation button
            student_icon = "☑️" if student_confirmed else "⬜"
            student_text = (
                "Я подтверждаю, что я студент" if lang == "ru" else "I confirm that I'm a student"
            )
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        f"{student_icon} {student_text}",
                        callback_data=f"moodle:toggle_student:{lang}",
                    )
                ]
            )

            # Terms agreement button
            terms_icon = "☑️" if terms_agreed else "⬜"
            terms_text = (
                "Я понимаю и соглашаюсь с условиями выше"
                if lang == "ru"
                else "I understand and agree to the terms above"
            )
            keyboard_buttons.append(
                [
                    InlineKeyboardButton(
                        f"{terms_icon} {terms_text}", callback_data=f"moodle:toggle_terms:{lang}"
                    )
                ]
            )

            # Continue button (only enabled if both are checked)
            if terms_agreed and student_confirmed:
                continue_text = "✅ Продолжить" if lang == "ru" else "✅ Continue"
                keyboard_buttons.append(
                    [InlineKeyboardButton(continue_text, callback_data="moodle:finalize_agreement")]
                )

            cancel_text = "❌ Отмена" if lang == "ru" else "❌ Cancel"
            keyboard_buttons.append(
                [InlineKeyboardButton(cancel_text, callback_data="moodle:menu")]
            )

            keyboard = InlineKeyboardMarkup(keyboard_buttons)

            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "finalize_agreement":
        # Handle completion of agreement (both boxes checked)
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            # Verify both are checked
            if not user.moodle_session_agreement or not user.moodle_student_confirmation:
                await query.answer("⚠️ Please confirm both statements first.", show_alert=True)
                return

            # Show the add session instructions
            active_sessions = get_active_sessions_for_user(db, user.id)
            session_count = len(active_sessions)
            max_sessions = 5

            text = (
                f"✅ *Agreement Confirmed*\n\n"
                f"*Current sessions:* {session_count}/{max_sessions}\n\n"
                f"*How to get your session credentials:*\n\n"
                f"1. Open Moodle in browser and log in\n"
                f"2. Press F12 → Application/Storage → Cookies\n"
                f"3. Copy `MoodleSession` cookie value\n"
                f"4. In Console, run: `M.cfg.sesskey`\n"
                f"5. Copy the sesskey value\n\n"
                f"*Then send:*\n"
                '`/moodle_add {"sesskey": "abc123", "moodleSession": "xyz789", "name": "My Session"}`\n\n'
                f"*Note:* Maximum {max_sessions} active sessions allowed."
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 My Sessions", callback_data="moodle:sessions")],
                    [InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")],
                ]
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "add_session":
        # Check current session count and show instructions
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            active_sessions = get_active_sessions_for_user(db, user.id)
            session_count = len(active_sessions)
            max_sessions = 5

            # Check if user has agreed to terms
            if not user.moodle_session_agreement or not user.moodle_student_confirmation:
                # Default to English
                lang = "en"
                text = get_security_notice_text(lang)

                # Build keyboard with checkboxes
                terms_agreed = user.moodle_session_agreement
                student_confirmed = user.moodle_student_confirmation

                keyboard_buttons = []

                # Language toggle button
                lang_button_text = "На русском"
                lang_toggle = "ru"
                keyboard_buttons.append(
                    [
                        InlineKeyboardButton(
                            lang_button_text, callback_data=f"moodle:switch_lang:{lang_toggle}"
                        )
                    ]
                )

                # Student confirmation button
                student_icon = "☑️" if student_confirmed else "⬜"
                keyboard_buttons.append(
                    [
                        InlineKeyboardButton(
                            f"{student_icon} I confirm that I'm a student",
                            callback_data=f"moodle:toggle_student:{lang}",
                        )
                    ]
                )

                # Terms agreement button
                terms_icon = "☑️" if terms_agreed else "⬜"
                keyboard_buttons.append(
                    [
                        InlineKeyboardButton(
                            f"{terms_icon} I understand and agree to the terms above",
                            callback_data=f"moodle:toggle_terms:{lang}",
                        )
                    ]
                )

                # Continue button (only enabled if both are checked)
                if terms_agreed and student_confirmed:
                    keyboard_buttons.append(
                        [
                            InlineKeyboardButton(
                                "✅ Continue", callback_data="moodle:finalize_agreement"
                            )
                        ]
                    )

                keyboard_buttons.append(
                    [InlineKeyboardButton("❌ Cancel", callback_data="moodle:menu")]
                )

                keyboard = InlineKeyboardMarkup(keyboard_buttons)
            elif session_count >= max_sessions:
                text = (
                    f"➕ *Add Moodle Session*\n\n"
                    f"❌ You already have {session_count} active sessions (maximum: {max_sessions}).\n\n"
                    f"Please stop a session before adding a new one.\n\n"
                    f'Use "📋 My Sessions" to manage your sessions.'
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📋 My Sessions", callback_data="moodle:sessions")],
                        [
                            InlineKeyboardButton(
                                "⬅️ Back to Moodle Menu", callback_data="moodle:menu"
                            )
                        ],
                    ]
                )
            else:
                text = (
                    f"➕ *Add Moodle Session*\n\n"
                    f"*Current sessions:* {session_count}/{max_sessions}\n\n"
                    f"*How to get your session credentials:*\n\n"
                    f"1. Open Moodle in browser and log in\n"
                    f"2. Press F12 → Application/Storage → Cookies\n"
                    f"3. Copy `MoodleSession` cookie value\n"
                    f"4. In Console, run: `M.cfg.sesskey`\n"
                    f"5. Copy the sesskey value\n\n"
                    f"*Then send:*\n"
                    '`/moodle_add {"sesskey": "abc123", "moodleSession": "xyz789", "name": "My Session"}`\n\n'
                    f"*Note:* Maximum {max_sessions} active sessions allowed."
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📋 My Sessions", callback_data="moodle:sessions")],
                        [
                            InlineKeyboardButton(
                                "⬅️ Back to Moodle Menu", callback_data="moodle:menu"
                            )
                        ],
                    ]
                )

            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "refresh_settings":
        # Show refresh settings
        from filoutil.commands.session_refresh import get_settings_keyboard

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            current_interval = get_user_refresh_interval(db, user.id)
            minutes = current_interval // 60

            text = (
                f"⚙️ *Session Refresh Settings*\n\n"
                f"*Current refresh interval:* {minutes}m ({current_interval}s)\n\n"
                f"Select a new interval:"
            )

            keyboard = get_settings_keyboard(current_interval)
            # Replace back button - convert tuple to list, modify, then create new keyboard
            keyboard_list = list(keyboard.inline_keyboard)
            keyboard_list[-1] = [
                InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")
            ]
            keyboard = InlineKeyboardMarkup(keyboard_list)

            try:
                if query.message.photo:
                    await query.message.delete()
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                else:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="Markdown"
                    )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    elif action == "notifications":
        # Show notifications list
        from filoutil.commands.notifications import show_notifications_list

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await show_notifications_list(db, user.id, query, context, page=0)

    elif action == "notification_settings":
        # Show notification settings
        from filoutil.commands.notification_settings import (
            get_user_notification_settings,
            show_notification_settings,
        )

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            settings = get_user_notification_settings(db, user.id)
            await show_notification_settings(query, context, settings)

    elif action == "session":
        # Show individual session details
        from filoutil.commands.moodle.sessions import show_session_details

        try:
            if len(data) < 3:
                raise IndexError("Missing session ID")
            session_id = int(data[2])
            if session_id <= 0:
                raise ValueError("Invalid session ID")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await show_session_details(db, user.id, session_id, query, context)

    elif action == "stop_session":
        # Handle stopping a session
        from filoutil.commands.moodle.sessions import stop_session_callback

        try:
            if len(data) < 3:
                raise IndexError("Missing session ID")
            session_id = int(data[2])
            if session_id <= 0:
                raise ValueError("Invalid session ID")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

        try:
            with SessionLocal() as db:
                user = get_user_by_telegram_id(db, query.from_user.id)
                if not user:
                    try:
                        await query.edit_message_text("❌ User not found.")
                    except BadRequest:
                        pass
                    await query.answer("❌ User not found.", show_alert=True)
                    return

                await stop_session_callback(db, user.id, session_id, query, context)
        except Exception as e:
            logger.error(f"Error stopping session: {e}", exc_info=True)
            await query.answer("❌ Failed to stop session. Please try again.", show_alert=True)

    elif action == "edit_name":
        # Handle editing session name
        from filoutil.commands.moodle.sessions import edit_session_name_callback

        try:
            if len(data) < 3:
                raise IndexError("Missing session ID")
            session_id = int(data[2])
            if session_id <= 0:
                raise ValueError("Invalid session ID")
        except (ValueError, IndexError):
            await query.answer("❌ Invalid request.", show_alert=True)
            return

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            await edit_session_name_callback(db, user.id, session_id, query, context)

    elif action == "grades":
        # Handle grades menu and gradebook view
        from filoutil.commands.moodle.grades import show_gradebook, show_grades_menu

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            if len(data) == 2:
                # Show grades menu
                await show_grades_menu(db, user.id, query, context, page=0)
            elif len(data) >= 3:
                sub_action = data[2]
                if sub_action == "page":
                    # Handle pagination: moodle:grades:page:{page_number}
                    if len(data) >= 4:
                        try:
                            page = int(data[3])
                            await show_grades_menu(db, user.id, query, context, page=page)
                        except (ValueError, IndexError):
                            await query.answer("❌ Invalid page number.", show_alert=True)
                elif sub_action == "course":
                    # Show gradebook for a course: moodle:grades:course:{course_id}
                    if len(data) >= 4:
                        try:
                            course_id = int(data[3])
                            await show_gradebook(db, user.id, course_id, query, context)
                        except (ValueError, IndexError):
                            await query.answer("❌ Invalid course ID.", show_alert=True)
                elif sub_action == "menu":
                    # Back to grades menu
                    await show_grades_menu(db, user.id, query, context, page=0)

    elif action == "sync_courses":
        # Handle manual course sync
        from filoutil.moodle_cabinet.course_watcher import process_courses_for_user

        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, query.from_user.id)
            if not user:
                try:
                    await query.edit_message_text("❌ User not found.")
                except BadRequest:
                    pass
                return

            # Get user's active sessions
            active_sessions = get_active_sessions_for_user(db, user.id)

            if not active_sessions:
                text = (
                    "📊 *Sync Courses & Grades*\n\n"
                    "❌ You don't have any active Moodle sessions.\n\n"
                    'Use "➕ Add Session" to create a new session first.'
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "➕ Add Session", callback_data="moodle:add_session"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ Back to Moodle Menu", callback_data="moodle:menu"
                            )
                        ],
                    ]
                )
                try:
                    await query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode="Markdown"
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return

            # Show processing message
            text = (
                f"📊 *Syncing Courses & Grades*\n\n"
                f"Processing {len(active_sessions)} active session(s)...\n\n"
                f"⏳ Please wait..."
            )
            try:
                await query.edit_message_text(text, parse_mode="Markdown")
            except BadRequest:
                pass

            # Track processed user IDs to avoid processing the same user multiple times
            # (users can have multiple sessions from different devices)
            processed_user_ids = set()
            processed_count = 0
            error_count = 0
            skipped_count = 0
            total_grades_changed = 0
            total_grades_unchanged = 0

            for session in active_sessions:
                # Skip if this user was already processed (multiple sessions for same user)
                if session.user_id in processed_user_ids:
                    skipped_count += 1
                    continue

                processed_user_ids.add(session.user_id)
                try:
                    stats = await process_courses_for_user(context.application, user.id, session)
                    processed_count += 1
                    total_grades_changed += stats.get("grades_changed", 0)
                    total_grades_unchanged += stats.get("grades_unchanged", 0)
                except Exception as e:
                    logger.error(
                        f"Error syncing courses for session {session.id}: {e}", exc_info=True
                    )
                    error_count += 1

            # Show result
            if error_count == 0:
                text = (
                    f"✅ *Sync Complete*\n\n"
                    f"Successfully processed {processed_count} session(s).\n"
                )
                if skipped_count > 0:
                    text += f"Skipped {skipped_count} duplicate session(s).\n"
                text += f"\n*Grades:*\n"
                text += f"Changed: {total_grades_changed}\n"
                text += f"Unchanged: {total_grades_unchanged}\n"
                text += f"\nYour courses and grades have been synced."
            else:
                text = (
                    f"⚠️ *Sync Complete*\n\n"
                    f"Processed: {processed_count} session(s)\n"
                    f"Errors: {error_count} session(s)\n"
                )
                if skipped_count > 0:
                    text += f"Skipped: {skipped_count} duplicate session(s)\n"
                text += f"\n*Grades:*\n"
                text += f"Changed: {total_grades_changed}\n"
                text += f"Unchanged: {total_grades_unchanged}\n"
                text += f"\nCheck logs for details."

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Moodle Menu", callback_data="moodle:menu")]]
            )
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
