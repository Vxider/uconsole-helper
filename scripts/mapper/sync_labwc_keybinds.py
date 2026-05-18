#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import os
import re
import sys


BEGIN_MARKER = "<!-- uconsole-helper-mapper:keybinds begin -->"
END_MARKER = "<!-- uconsole-helper-mapper:keybinds end -->"
DEFAULT_BLOCK_FILE = Path(__file__).resolve().with_name("labwc-keybinds.xml")


def default_block() -> str:
    home_bin = os.path.expanduser("~/.local/bin")
    return "\n".join(
        [
            BEGIN_MARKER,
            '<keybind key="S-Return">',
            f'  <action name="Execute" command="{home_bin}/shift-enter-newline" />',
            "</keybind>",
            END_MARKER,
            "",
        ]
    )


def indent_block(block: str, item_indent: str) -> str:
    action_indent = item_indent + "  "
    lines: list[str] = []
    for line in block.strip().splitlines():
        if line.startswith("  <action "):
            lines.append(f"{action_indent}{line[2:]}")
        elif line:
            lines.append(f"{item_indent}{line}")
        else:
            lines.append("")
    return "\n".join(lines)


DEFAULT_RC_XML = f"""<?xml version="1.0"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <keyboard>
{indent_block(default_block(), "    ")}
  </keyboard>
</openbox_config>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.config/labwc/rc.xml").expanduser(),
    )
    parser.add_argument(
        "--block-file",
        type=Path,
        default=DEFAULT_BLOCK_FILE,
    )
    return parser.parse_args()


def backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.bak.{stamp}")


def load_block(path: Path) -> str:
    if not path.exists():
        return default_block()
    text = path.read_text(encoding="utf-8").strip()
    if BEGIN_MARKER not in text or END_MARKER not in text:
        raise ValueError(f"block file missing markers: {path}")
    return text + "\n"


def replace_managed_block(text: str, block: str) -> tuple[str, str]:
    if BEGIN_MARKER in text and END_MARKER in text:
        marker_match = re.search(rf"(?m)^([ \t]*){re.escape(BEGIN_MARKER)}$", text)
        item_indent = marker_match.group(1) if marker_match else "    "
        pattern = re.compile(
            rf"(?ms)^[ \t]*{re.escape(BEGIN_MARKER)}\n.*?^[ \t]*{re.escape(END_MARKER)}$",
        )
        return pattern.sub(indent_block(block, item_indent), text, count=1), "updated"

    if "shift-enter-newline" in text:
        return text, "skipped-existing"

    keyboard_close = re.search(r"(?m)^([ \t]*)</keyboard>\s*$", text)
    if keyboard_close:
        keyboard_indent = keyboard_close.group(1)
        item_indent = keyboard_indent + "  "
        replacement = f"{indent_block(block, item_indent)}\n{keyboard_indent}</keyboard>"
        return text[: keyboard_close.start()] + replacement + text[keyboard_close.end() :], "inserted"

    root_close = re.search(r"(?m)^([ \t]*)</(?:openbox_config|labwc_config)>\s*$", text)
    if root_close:
        root_indent = root_close.group(1)
        keyboard_indent = root_indent + "  "
        item_indent = keyboard_indent + "  "
        insertion = f"{keyboard_indent}<keyboard>\n{indent_block(block, item_indent)}\n{keyboard_indent}</keyboard>\n"
        return text[: root_close.start()] + insertion + text[root_close.start() :], "inserted"

    raise ValueError("labwc rc.xml has no <keyboard> or root closing tag")


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser()
    block = load_block(args.block_file)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if not config_path.exists():
        config_path.write_text(DEFAULT_RC_XML, encoding="utf-8")
        print(f"created {config_path}")
        return 0

    original = config_path.read_text(encoding="utf-8")
    updated, status = replace_managed_block(original, block)

    if status == "skipped-existing":
        print(f"skipped {config_path}: existing Shift+Enter keybind detected")
        return 0

    if updated == original:
        print(f"unchanged {config_path}")
        return 0

    backup = backup_path(config_path)
    backup.write_text(original, encoding="utf-8")
    config_path.write_text(updated, encoding="utf-8")
    print(f"{status} {config_path}")
    print(f"backup {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
