"""Compatibility imports for terminal input ownership."""

from .frontends.terminal.io import TerminalInputSilencer, silenced_terminal_input, terminal_input_prompt

__all__ = ["TerminalInputSilencer", "silenced_terminal_input", "terminal_input_prompt"]
