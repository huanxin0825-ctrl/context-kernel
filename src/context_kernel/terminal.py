from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import unicodedata


def chat_width() -> int:
    return max(88, min(shutil.get_terminal_size((112, 20)).columns, 132))


def chat_color(text: str, color: str, *, bold: bool = False) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    codes = {
        "blue": "34",
        "cyan": "36",
        "green": "32",
        "magenta": "35",
        "white": "37",
        "yellow": "33",
        "red": "31",
        "dim": "2",
        "bold": "1",
    }
    selected: list[str] = []
    if bold:
        selected.append("1")
    selected.append(codes.get(color, "0"))
    return f"\033[{';'.join(selected)}m{text}\033[0m"


def chat_banner(title: str, subtitle: str) -> None:
    width = chat_width()
    print("")
    print(chat_color("=" * width, "cyan", bold=True))
    print(chat_color(title, "cyan", bold=True))
    print(chat_color(subtitle, "dim"))
    print(chat_color("=" * width, "cyan", bold=True))


def chat_rule(title: str) -> str:
    width = chat_width()
    label = f" {title} "
    remaining = max(0, width - len(label))
    left = remaining // 2
    right = remaining - left
    return chat_color("-" * left + label + "-" * right, "cyan")


def chat_panel(title: str, rows: list[tuple[str, str]]) -> None:
    width = chat_width()
    print("")
    print(chat_color(f"[ {title} ]", "cyan", bold=True))
    key_width = max(len(key) for key, _ in rows)
    for key, value in rows:
        prefix = f"  {key:<{key_width}}  "
        wrapped = wrap_chat_text(str(value), indent=" " * len(prefix), width=width)
        lines = wrapped.splitlines() or [""]
        print(chat_color(prefix, "dim") + lines[0].lstrip())
        for line in lines[1:]:
            print(line)


def chat_notice(title: str, message: str) -> None:
    print("")
    print(chat_color(f"! {title}", "yellow", bold=True))
    print(wrap_chat_text(message, indent="  "))


def wrap_chat_text(text: str, *, indent: str = "", width: int | None = None) -> str:
    width = width or chat_width()
    usable = max(30, width - len(indent))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append(indent.rstrip())
            continue
        current = words[0]
        for word in words[1:]:
            if len(current) + 1 + len(word) > usable:
                lines.append(indent + current)
                current = word
            else:
                current += " " + word
        lines.append(indent + current)
    return "\n".join(lines)


def wrap_plain(text: str, *, width: int) -> str:
    return "\n".join(wrap_chat_text(text, width=width).splitlines())


def tui_rule(title: str, width: int) -> str:
    text = title[:width]
    remaining = max(0, width - len(text))
    left = remaining // 2
    right = remaining - left
    return "=" * left + text + "=" * right


def tui_soft_rule(title: str, width: int) -> str:
    label = f" {title} "
    remaining = max(0, width - len(label))
    left = min(10, remaining // 3)
    right = max(0, remaining - left)
    return "-" * left + label + "-" * right


def tui_pill(text: str, width: int | None = None) -> str:
    value = f"[ {text} ]"
    return truncate_line(value, width) if width else value


def truncate_line(text: str, width: int) -> str:
    value = str(text)
    if display_width(value) <= width:
        return value
    if width <= 3:
        return "." * max(0, width)
    result = ""
    used = 0
    for char in value:
        char_width = char_display_width(char)
        if used + char_width > width - 3:
            break
        result += char
        used += char_width
    return result + "..."


def pad_display(text: str, width: int) -> str:
    value = truncate_line(text, width)
    return value + " " * max(0, width - display_width(value))


def display_width(text: str) -> int:
    return sum(char_display_width(char) for char in str(text))


def char_display_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def compact_path(path: Path) -> str:
    text = str(path)
    width = chat_width() - 18
    if len(text) <= width:
        return text
    return "..." + text[-max(12, width - 3) :]


def ascii_meter(value: int, limit: int, width: int) -> str:
    cells = max(4, width)
    filled = min(cells, max(0, int(round((value / max(1, limit)) * cells))))
    return "[" + "#" * filled + "." * (cells - filled) + "]"
