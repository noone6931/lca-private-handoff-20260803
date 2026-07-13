"""Provider-neutral terminal-content policy for assistant responses.

This owner keeps malformed or contentless provider replies out of the primary
transcript.  It deliberately classifies response *shape*, not business terms or
provider-specific placeholder words.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_CODE_FENCE = re.compile(r"^\s*```", re.DOTALL)
_OUTER_EMPHASIS = re.compile(r"^(?:\*\*|__|\*|_)(?P<inner>.+?)(?:\*\*|__|\*|_)$")
_DIRECT_ANSWER_ATOMS = frozenset({"done", "complete", "completed", "ok", "okay", "yes", "no", "true", "false"})


@dataclass(frozen=True)
class TerminalContentAssessment:
    """Whether a no-tool assistant response is safe to treat as an answer."""

    substantive: bool
    kind: str


def assess_terminal_content(
    content: object,
    *,
    request: str,
    forced_final: bool = False,
) -> TerminalContentAssessment:
    """Classify a terminal response without inspecting repository-specific terms.

    A real explanation, a code snippet, and a direct one-word answer related to
    the request remain valid.  What is retried is an empty/punctuation-only
    response or a detached, presentation-only opaque token such as an adapter
    placeholder.  The latter rule is intentionally structural: it does not name
    provider artifacts.
    """

    if not isinstance(content, str) or not content.strip():
        return TerminalContentAssessment(False, "empty_content")
    stripped = content.strip()
    if _CODE_FENCE.match(stripped) or (stripped.startswith("`") and stripped.endswith("`")):
        return TerminalContentAssessment(True, "code_content")
    visible = _strip_outer_emphasis(stripped)
    if any("\u4e00" <= character <= "\u9fff" for character in visible):
        return TerminalContentAssessment(True, "text_content")
    words = _WORD.findall(visible)
    if not words:
        return TerminalContentAssessment(False, "presentation_only")
    if len(words) > 1:
        return TerminalContentAssessment(True, "text_content")
    token = words[0].casefold()
    if token in _DIRECT_ANSWER_ATOMS or token in {word.casefold() for word in _WORD.findall(request or "")}:
        return TerminalContentAssessment(True, "direct_answer")
    # An ordinary one-word response can be a valid lightweight acknowledgement
    # or tool-recovery answer. In a forced-final turn, however, it cannot carry
    # the requested bounded delivery and is retried without naming any provider
    # artifact. Presentation-only wrappers are never substantive in either phase.
    if (visible != stripped or forced_final) and visible.casefold() == token and len(token) <= 32:
        return TerminalContentAssessment(False, "detached_single_token")
    return TerminalContentAssessment(True, "text_content")


def terminal_retry_message(*, attempt: int, max_attempts: int, forced_final: bool) -> str:
    phase = "the required final answer" if forced_final else "the current answer"
    return (
        "[Runtime provider recovery]\n"
        f"The provider returned no usable {phase}. Reply with a substantive answer now. "
        f"Do not emit placeholders, empty formatting, or tool protocol text. Attempt {attempt}/{max_attempts}."
    )


def _strip_outer_emphasis(value: str) -> str:
    current = value
    while True:
        match = _OUTER_EMPHASIS.fullmatch(current)
        if match is None:
            return current
        current = match.group("inner").strip()
