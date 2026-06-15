"""Minimal TUI test - run this to verify Chinese input works.

Usage: PYTHONPATH=python python scripts/test_tui_input.py
"""
from textual.app import App, ComposeResult
from textual.widgets import Static, Header, Footer


class TaskSubmitted(Exception):
    """Quick message class for testing."""
    def __init__(self, text):
        self.text = text


class CJKInput(Static):
    """CJK-compatible input using raw key capture."""
    DEFAULT_CSS = """
    CJKInput {
        height: 3;
        margin: 1 1 0 1;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, *a, **kw):
        Static.__init__(self, "", *a, **kw)
        self._buffer = ""

    def _on_mount(self):
        self._render()

    def _render(self):
        if self._buffer:
            self.update(f"[bold cyan]>[/bold cyan] {self._buffer}█")
        else:
            self.update("[bold cyan]>[/bold cyan] [dim]输入中文试试... (press Enter to submit)[/dim]█")

    def _on_key(self, event):
        if event.key == "enter":
            text = self._buffer.strip()
            if text:
                # Show result in the output area
                app = self.app
                output = app.query_one("#output", Static)
                output.update(f"You typed: [bold green]{text}[/bold green]")
            self._buffer = ""
            self._render()
            event.prevent_default()
            event.stop()
            return

        if event.key in ("backspace", "delete"):
            if self._buffer:
                self._buffer = self._buffer[:-1]
                self._render()
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+u":
            self._buffer = ""
            self._render()
            event.prevent_default()
            event.stop()
            return

        character = event.character
        if character and event.is_printable:
            self._buffer += character
            self._render()
            event.prevent_default()
            event.stop()


class TestApp(App):
    """Test CJK input rendering."""

    TITLE = "CJK Input Test"
    CSS = "Screen { layout: vertical; }"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Try typing Chinese characters below:", id="label")
        yield CJKInput(id="input")
        yield Static("Result will appear here", id="output")
        yield Footer()


if __name__ == "__main__":
    app = TestApp()
    app.run()
