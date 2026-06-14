"""TUI package for UltimateCoders sandbox mode.

Provides a Textual-based terminal UI with:
- Logo header with ASCII art + version
- Conversational chat log (user input + system output)
- Subtask tree with progress indicators
- Task submission input
- Status bar

Requires optional dependencies: textual>=0.40, rich>=13.0
"""

from __future__ import annotations

from ultimate_coders.tui.app import SandboxTUI

__all__ = ["SandboxTUI"]
