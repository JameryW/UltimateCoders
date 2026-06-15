"""Custom Textual widgets for the sandbox TUI.

Widgets:
- LogoHeader: ASCII art logo + version display
- SubtaskTree: Tree view showing task -> subtasks with status icons
- ChatLog: Conversational-style scrollable log (user input + system output)
- TaskInput: Input field for submitting new tasks
- StatusBar: Compact status bar showing worker/backend/progress
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode

from ultimate_coders.agent.types import Subtask, SubtaskStatus

# Status icons for subtasks
_STATUS_ICONS: dict[SubtaskStatus, str] = {
    SubtaskStatus.PENDING: "[dim]⏳[/]",
    SubtaskStatus.ASSIGNED: "[dim]⏳[/]",
    SubtaskStatus.IN_PROGRESS: "[bold cyan]🔄[/]",
    SubtaskStatus.COMPLETED: "[bold green]✅[/]",
    SubtaskStatus.FAILED: "[bold red]❌[/]",
    SubtaskStatus.CONFLICTED: "[bold yellow]⚠️[/]",
}


def _get_version() -> str:
    """Read version from ultimate_coders package, fallback to '0.1.0'."""
    try:
        from ultimate_coders import __version__
        return __version__
    except Exception:
        return "0.1.0"


class LogoHeader(Static):
    """Compact ASCII art logo header with version number.

    Renders a 2-line box-drawing 'UC' monogram alongside the
    'UltimateCoders' name and version. Docked at the top of the screen.
    """

    DEFAULT_CSS = """
    LogoHeader {
        dock: top;
        height: 3;
        width: 100%;
        background: $surface;
        color: $text;
        padding: 0 2;
        content-align: left middle;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the LogoHeader with the ASCII art + version."""
        version = _get_version()
        # Compact 2-line box-drawing "UC" monogram
        logo = (
            f"[bold cyan]╔═╗╦ ╦╔═╗╔═╗[/]   "
            f"[bold]UltimateCoders[/] [dim]v{version}[/]\n"
            f"[bold cyan]║  ╚╦╝║╣ ╚═╗[/]\n"
            f"[bold cyan]╚═╝ ╩ ╚═╝╚═╝[/]"
        )
        super().__init__(logo, *args, **kwargs)


class SubtaskTree(Tree):
    """Tree widget showing task -> subtasks with progress icons.

    Displays a root node for the task with child nodes for each subtask.
    Status icons update in real-time as subtasks progress through
    pending -> in_progress -> completed/failed states.
    """

    DEFAULT_CSS = """
    SubtaskTree {
        height: 1fr;
        border: solid $primary;
        border-title-color: $text;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:
        """Initialize the SubtaskTree with an empty root node."""
        super().__init__("No task", *args, **kwargs)
        self._task_id: str = ""
        self._subtask_nodes: dict[str, TreeNode] = {}

    def set_task(self, task_description: str, task_id: str) -> None:
        """Set the root task node.

        Args:
            task_description: Human-readable task description.
            task_id: Task UUID.
        """
        self._task_id = task_id
        self._subtask_nodes.clear()
        self.root.set_label(Text(task_description))
        self.root.remove_children()
        self.expand()

    def add_subtask(self, subtask: Subtask, index: int) -> None:
        """Add a subtask node to the tree.

        Args:
            subtask: The Subtask object.
            index: 1-based index for display.
        """
        icon = _STATUS_ICONS.get(subtask.status, "⏳")
        label = f"{icon} {index}. {subtask.description}"
        node = self.root.add(label, expand=True)
        self._subtask_nodes[subtask.id] = node

    def update_subtask_status(self, subtask_id: str, status: SubtaskStatus) -> None:
        """Update the status icon for a subtask.

        Args:
            subtask_id: The subtask ID.
            status: New status.
        """
        node = self._subtask_nodes.get(subtask_id)
        if node is None:
            return
        icon = _STATUS_ICONS.get(status, "⏳")
        # Extract the index and description from the existing label
        old_label = str(node.label)
        # Label format: "icon N. description" -- extract after the first space
        parts = old_label.split(" ", 1)
        if len(parts) > 1:
            rest = parts[1]
        else:
            rest = old_label
        node.set_label(f"{icon} {rest}")

    def update_progress(self, completed: int, total: int) -> None:
        """Update the tree title with progress info.

        Args:
            completed: Number of completed subtasks.
            total: Total number of subtasks.
        """
        pct = int(100 * completed / total) if total > 0 else 0
        self.border_title = f"Subtasks [{completed}/{total} {pct}%]"


class ChatLog(Static):
    """Conversational-style scrollable output log.

    Shows timestamped entries in a chat-like format:
    - User input: prefixed with '>' in cyan
    - System output: plain timestamped messages

    Auto-scrolls to bottom but can be scrolled up to view history.
    """

    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        border: solid $primary;
        border-title-color: $text;
        padding: 0 1;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the ChatLog with an empty line buffer."""
        super().__init__("", *args, **kwargs)
        self._lines: list[str] = []
        self._max_lines: int = 2000

    def on_mount(self) -> None:
        """Set the border title when the widget is mounted."""
        self.border_title = "Chat"

    def append_user_input(self, message: str) -> None:
        """Append a user input entry with '>' prefix.

        Args:
            message: The user's input text.
        """
        now = datetime.now(timezone.utc)
        ts = now.strftime("%H:%M:%S")
        line = f"[dim][{ts}][/dim] [bold cyan]> [/bold cyan]{message}"
        self._lines.append(line)
        if len(self._lines) > self._max_lines:
            self._lines = self._lines[-self._max_lines:]
        self.update("\n".join(self._lines))
        self.scroll_end(animate=False)

    def append(self, message: str, style: str = "") -> None:
        """Append a system output entry.

        Args:
            message: The log message text.
            style: Optional Rich style string (e.g., "bold green", "dim").
        """
        now = datetime.now(timezone.utc)
        ts = now.strftime("%H:%M:%S")
        if style:
            line = f"[dim][{ts}][/dim] [{style}]{message}[/{style}]"
        else:
            line = f"[dim][{ts}][/dim] {message}"

        self._lines.append(line)
        if len(self._lines) > self._max_lines:
            self._lines = self._lines[-self._max_lines:]

        self.update("\n".join(self._lines))
        # Auto-scroll to bottom
        self.scroll_end(animate=False)

    def clear_log(self) -> None:
        """Clear all log entries."""
        self._lines.clear()
        self.update("")


class TaskInput(Static, can_focus=True):
    """CJK-compatible input field using raw key capture.

    Textual's Input and TextArea widgets cannot render CJK characters
    in some terminal environments. This widget captures key events
    directly, manages an internal text buffer, and renders content
    via Rich (which handles CJK correctly).

    Submits on Enter.
    """

    DEFAULT_CSS = """
    TaskInput {
        height: 3;
        margin: 1 1 0 1;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
    }

    TaskInput:focus {
        border: solid $accent;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the TaskInput with an empty buffer."""
        Static.__init__(self, "", *args, **kwargs)
        self._buffer: str = ""

    def _on_mount(self) -> None:
        """Show prompt on mount."""
        self._render_prompt()

    def _on_focus(self, event) -> None:
        """Re-render with cursor when focused."""
        self._render_prompt()

    def _on_blur(self, event) -> None:
        """Re-render without cursor when blurred."""
        self._render_prompt()

    def _render_prompt(self) -> None:
        """Render the input with '>' prefix and cursor."""
        cursor = "█" if self.has_focus else ""
        if self._buffer:
            content = f"[bold cyan]>[/bold cyan] {self._buffer}{cursor}"
        else:
            placeholder = "[dim]type task description and press Enter...[/dim]" if self.has_focus else ""
            content = f"[bold cyan]>[/bold cyan] {placeholder}{cursor}"
        self.update(content)

    def _on_key(self, event) -> None:
        """Handle key events for text input."""
        key = event.key

        # Enter: submit
        if key == "enter":
            text = self._buffer.strip()
            if text:
                self.post_message(TaskSubmitted(text))
            self._buffer = ""
            self._render_prompt()
            event.prevent_default()
            event.stop()
            return

        # Backspace: delete last char
        if key == "backspace":
            if self._buffer:
                self._buffer = self._buffer[:-1]
                self._render_prompt()
            event.prevent_default()
            event.stop()
            return

        # Delete: same as backspace
        if key == "delete":
            if self._buffer:
                self._buffer = self._buffer[:-1]
                self._render_prompt()
            event.prevent_default()
            event.stop()
            return

        # Ctrl+U: clear line
        if key == "ctrl+u":
            self._buffer = ""
            self._render_prompt()
            event.prevent_default()
            event.stop()
            return

        # Printable character (includes CJK via IME)
        character = event.character
        if character and event.is_printable:
            self._buffer += character
            self._render_prompt()
            event.prevent_default()
            event.stop()
            return


class StatusBar(Static):
    """Compact status bar showing worker ID, backend, and progress.

    Displays single-line status information at the bottom of the screen,
    including the current worker ID, sandbox backend, and subtask progress.
    """

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        width: 100%;
        background: $primary;
        color: $text;
        padding: 0 1;
        content-align: left middle;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the StatusBar with default values."""
        super().__init__("", *args, **kwargs)
        self._worker_id: str = ""
        self._backend: str = "subprocess"
        self._progress: str = "0/0"

    def configure(self, worker_id: str, backend: str) -> None:
        """Set the worker ID and backend.

        Args:
            worker_id: The sandbox worker identifier.
            backend: The sandbox backend (subprocess or docker).
        """
        self._worker_id = worker_id
        self._backend = backend
        self._update_content()

    def set_progress(self, completed: int, total: int) -> None:
        """Update the progress indicator.

        Args:
            completed: Number of completed subtasks.
            total: Total number of subtasks.
        """
        self._progress = f"{completed}/{total}"
        self._update_content()

    def _update_content(self) -> None:
        """Update the widget content with current status values."""
        content = (
            f" Worker: {self._worker_id} | "
            f"Backend: {self._backend} | "
            f"Progress: {self._progress}"
        )
        try:
            self.update(content)
        except Exception:
            # Widget not yet mounted — will render on mount
            pass


from textual.message import Message


class TaskSubmitted(Message):
    """Message emitted when the user submits a task via TaskInput."""

    def __init__(self, text: str) -> None:
        """Initialize with the submitted task description."""
        Message.__init__(self)
        self.text = text
