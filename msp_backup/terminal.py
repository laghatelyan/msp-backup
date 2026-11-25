"""Terminal rendering and keyboard navigation."""

import os
import shutil
import sys


class TerminalUI:
    """Small ANSI terminal presentation layer with a plain-text fallback."""

    COLORS = {"blue": "34", "cyan": "36", "green": "32", "yellow": "33", "red": "31", "muted": "90", "bold": "1"}

    def __init__(self):
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.color = self.interactive and "NO_COLOR" not in os.environ

    def paint(self, value, *styles):
        if not self.color:
            return str(value)
        codes = ";".join(self.COLORS[style] for style in styles if style in self.COLORS)
        return f"\033[{codes}m{value}\033[0m" if codes else str(value)

    def clear(self):
        if self.interactive:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    def header(self, title, subtitle=""):
        self.clear()
        print(self.paint("━" * 72, "blue"))
        print(self.paint(f"  {title}", "bold", "blue"))
        if subtitle:
            print(self.paint(f"  {subtitle}", "muted"))
        print(self.paint("━" * 72, "blue"))

    def section(self, title):
        print(f"\n{self.paint('›', 'cyan', 'bold')} {self.paint(title, 'bold')}")

    def status(self, message, state="running"):
        icon, style = {"running": ("●", "cyan"), "success": ("✓", "green"), "error": ("✗", "red"), "warning": ("!", "yellow")}.get(state, ("•", "muted"))
        print(f"{self.paint(icon, style, 'bold')} {message}", flush=True)

    def menu_item(self, key, label, description=""):
        line = f"  {self.paint(key.rjust(2), 'cyan', 'bold')}  {label}"
        if description:
            line += f"  {self.paint(description, 'muted')}"
        print(line)

    @staticmethod
    def _read_key():
        """Read exactly one key event so held-arrow repeats are not swallowed."""
        import select
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)

        def read_available(timeout):
            readable, _, _ = select.select([descriptor], [], [], timeout)
            return os.read(descriptor, 1) if readable else b""

        try:
            tty.setraw(descriptor)
            first = os.read(descriptor, 1)
            if first != b"\x1b":
                return first.decode("utf-8", errors="ignore")

            second = read_available(0.25)
            if second not in (b"[", b"O"):
                return (first + second).decode("utf-8", errors="ignore")

            sequence = bytearray(first + second)
            while len(sequence) < 8:
                value = read_available(0.10)
                if not value:
                    break
                sequence.extend(value)
                # CSI and SS3 sequences end at their first final byte. Stop here
                # instead of draining the input queue, which may contain repeats.
                if 0x40 <= value[0] <= 0x7E:
                    break
            return sequence.decode("utf-8", errors="ignore")
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)

    def select(self, title, options, subtitle="", initial=0):
        """Select an option with arrow keys, falling back to numbered input."""
        options = list(options)
        if not options:
            return None
        if not self.interactive:
            self.header(title, subtitle)
            for key, label, *rest in options:
                self.menu_item(key, label, rest[0] if rest else "")
            valid = {str(key) for key, *_ in options}
            while True:
                value = input("Select an option: ").strip()
                if value in valid:
                    return value
                self.status("Choose one of the listed options.", "error")

        selected = max(0, min(initial, len(options) - 1))
        keys = {str(option[0]) for option in options}
        visible_count = max(5, shutil.get_terminal_size((80, 24)).lines - 7)
        while True:
            self.header(title, subtitle)
            start = min(max(selected - visible_count + 1, 0), max(0, len(options) - visible_count))
            end = min(len(options), start + visible_count)
            for index in range(start, end):
                key, label, *rest = options[index]
                description = rest[0] if rest else ""
                marker = self.paint("❯", "cyan", "bold") if index == selected else " "
                line = f" {marker} {self.paint(str(key).rjust(2), 'cyan', 'bold')}  {label}"
                if description:
                    line += f"  {self.paint(description, 'muted')}"
                print(line)
            if len(options) > visible_count:
                print(f"\nShowing {start + 1}–{end} of {len(options)}")
            print(f"{self.paint('↑/↓ ←/→', 'cyan', 'bold')} navigate   {self.paint('Enter', 'cyan', 'bold')} select   {self.paint('PgUp/PgDn', 'cyan', 'bold')} page   {self.paint('0/Esc', 'cyan', 'bold')} back")
            key = self._read_key()
            if key in ("\x1b[A", "\x1bOA"):
                selected = (selected - 1) % len(options)
            elif key in ("\x1b[B", "\x1bOB"):
                selected = (selected + 1) % len(options)
            elif key in ("\r", "\n"):
                return str(options[selected][0])
            elif key in ("\x03", "\x1b"):
                return "0" if "0" in keys else None
            elif key == "0" and "0" in keys:
                return "0"
            elif key in ("\x1b[C", "\x1bOC"):
                selected = (selected + 1) % len(options)
            elif key in ("\x1b[D", "\x1bOD"):
                selected = (selected - 1) % len(options)
            elif key == "\x1b[5~":
                selected = max(0, selected - visible_count)
            elif key == "\x1b[6~":
                selected = min(len(options) - 1, selected + visible_count)
            elif key in ("\x1b[H", "\x1b[1~"):
                selected = 0
            elif key in ("\x1b[F", "\x1b[4~"):
                selected = len(options) - 1

    def pause(self, prompt="Press any key to continue"):
        if self.interactive:
            print(f"\n{self.paint(prompt, 'muted')}", end="", flush=True)
            self._read_key()
            print()
        else:
            input(f"\n{prompt} (Enter) ")

