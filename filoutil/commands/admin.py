import asyncio
import logging
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from filoutil.db.postgres import SessionLocal, engine
from filoutil.db.users import get_user_by_telegram_id

logger = logging.getLogger(__name__)

# Get the project root directory (parent of filoutil package)
PROJECT_ROOT = Path(__file__).parent.parent.parent
FONT_PATH = PROJECT_ROOT / "assets" / "ubuntu_mono.ttf"


def text_to_png(src: str) -> BytesIO:
    """Convert text to PNG image using Ubuntu Mono font."""
    src = src[:10240]  # Limit to 10KB
    if not src.strip():
        src = " "  # Ensure at least one character for dimension calculation

    file = BytesIO()
    font_size = 24
    font = ImageFont.truetype(str(FONT_PATH), font_size)
    with Image.new("RGB", (0, 0)) as img:
        text_bbox = ImageDraw.Draw(img).multiline_textbbox((0, 0), src, font=font)
    padding = 20
    image_width = max(text_bbox[2] - text_bbox[0] + padding, 100)  # Minimum 100px width
    image_height = max(
        text_bbox[3] - text_bbox[1] + int(padding * 1.5), 100
    )  # Minimum 100px height
    image = Image.new("RGB", (image_width, image_height), color="black")
    draw = ImageDraw.Draw(image)
    draw.multiline_text((padding // 2, padding // 2), src, fill="white", font=font)
    image.save(file, "png")
    file.seek(0)
    return file


async def ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user is an admin. Works with both message and callback query updates."""
    # Handle callback queries
    if update.callback_query and update.callback_query.from_user:
        telegram_id = update.callback_query.from_user.id
        with SessionLocal() as db:
            user = get_user_by_telegram_id(db, telegram_id)
            if not user or user.role != "admin":
                await update.callback_query.answer(
                    "❌ This command is only available to admins.", show_alert=True
                )
                return False
        return True

    # Handle messages
    if update.message is None or update.message.from_user is None:
        return False

    telegram_id = update.message.from_user.id

    with SessionLocal() as db:
        user = get_user_by_telegram_id(db, telegram_id)
        if not user or user.role != "admin":
            await update.message.reply_text("❌ This command is only available to admins.")
            return False

    return True


async def send_command_output(update: Update, output: str, command_type: str = "Command") -> None:
    """Send command output as image + txt file, or as code-block if short."""
    if len(output) < 1500:
        # Send as code-block formatted text
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
        return

    # Generate image
    image_buf = text_to_png(output)
    image_buf.name = f"{command_type.lower()}_output.png"

    # Create txt file buffer
    txt_buf = BytesIO(output.encode("utf-8"))
    txt_buf.name = f"{command_type.lower()}_output.txt"

    # Send image as document
    image_buf.seek(0)
    await update.message.reply_document(
        document=image_buf,
        filename=image_buf.name,
        caption=f"📊 {command_type} Output (Image)",
    )

    # Send txt file
    txt_buf.seek(0)
    await update.message.reply_document(
        document=txt_buf,
        filename=txt_buf.name,
        caption=f"📄 {command_type} Output (Text File)",
    )


async def shell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a shell command and return the output."""
    if not await ensure_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/shell <command>`\nExample: `/shell ls -la`", parse_mode="Markdown"
        )
        return

    command = " ".join(context.args)

    try:
        # Run command with timeout
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024,  # 1MB limit
        )

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            await update.message.reply_text("❌ Command timed out after 30 seconds.")
            return

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        exit_code = process.returncode

        # Format output with command and exit code
        formatted_output = f"$ {command}\n\n{output}"
        if exit_code != 0:
            formatted_output += f"\n\n[Exit code: {exit_code}]"

        await send_command_output(update, formatted_output, "Shell Command")

    except Exception as e:
        error_msg = f"Error running command: {str(e)}"
        logger.exception("Error in shell_command")
        await update.message.reply_text(f"❌ {error_msg}")


async def db_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a database query and return the output."""
    if not await ensure_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/db <sql_query>`\nExample: `/db SELECT * FROM users LIMIT 10`",
            parse_mode="Markdown",
        )
        return

    query_str = " ".join(context.args)

    try:
        # Use begin() for automatic transaction management
        with engine.begin() as conn:
            result = conn.execute(text(query_str))

            # Try to fetch rows (for SELECT queries)
            try:
                rows = result.fetchall()
                # Get column names
                columns = result.keys()
                col_names = list(columns)

                # Format output
                if not rows:
                    output = f"SQL: {query_str}\n\nNo rows returned."
                else:
                    # Format as table
                    output_lines = [f"SQL: {query_str}\n"]
                    output_lines.append("\n" + " | ".join(col_names))
                    output_lines.append("-" * (len(" | ".join(col_names))))

                    for row in rows:
                        row_str = " | ".join(str(val) if val is not None else "NULL" for val in row)
                        output_lines.append(row_str)

                    output = "\n".join(output_lines)

                    # Add row count
                    output += f"\n\n[{len(rows)} row(s) returned]"
            except Exception:
                # Non-SELECT query (INSERT, UPDATE, DELETE, etc.)
                rowcount = result.rowcount
                output = f"SQL: {query_str}\n\nQuery executed successfully.\n[{rowcount} row(s) affected]"

            await send_command_output(update, output, "Database Query")

    except Exception as e:
        error_msg = f"Error running query: {str(e)}"
        logger.exception("Error in db_query")
        await update.message.reply_text(f"❌ {error_msg}")
