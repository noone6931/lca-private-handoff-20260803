"""Compatibility imports for shared terminal input ownership."""

from ...platform.terminal import TerminalInputSilencer, silenced_terminal_input, terminal_input_prompt

__all__ = ["TerminalInputSilencer", "silenced_terminal_input", "terminal_input_prompt"]
