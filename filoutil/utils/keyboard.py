"""Utility functions for creating Telegram inline keyboards."""

from telegram import InlineKeyboardButton


def arrange_buttons_in_rows(
    buttons: list[InlineKeyboardButton], buttons_per_row: int = 3
) -> list[list[InlineKeyboardButton]]:
    """
    Arrange a list of buttons into rows with a specified number of buttons per row.

    Args:
        buttons: List of InlineKeyboardButton objects
        buttons_per_row: Number of buttons per row (default: 3)

    Returns:
        List of rows, where each row is a list of InlineKeyboardButton objects

    Example:
        >>> buttons = [InlineKeyboardButton(str(i), callback_data=str(i)) for i in range(5)]
        >>> rows = arrange_buttons_in_rows(buttons, buttons_per_row=3)
        >>> # Returns: [[btn1, btn2, btn3], [btn4, btn5]]
    """
    rows = []
    for i in range(0, len(buttons), buttons_per_row):
        row = buttons[i : i + buttons_per_row]
        rows.append(row)
    return rows
