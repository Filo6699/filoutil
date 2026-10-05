"""Lossless text pagination within Telegram's UTF-16 message limit."""


def split_message(text: str, limit: int = 3500) -> list[str]:
    """Prefer line boundaries; account for emoji consuming two UTF-16 units."""
    pages = []
    while text:
        units = 0
        end = 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end += 1
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline > 0:
                end = newline + 1
        pages.append(text[:end])
        text = text[end:]
    return pages or [""]
