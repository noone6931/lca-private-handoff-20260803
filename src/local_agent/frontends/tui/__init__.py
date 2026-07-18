"""Full-screen terminal frontend with a single synchronous Runtime worker."""

from .mailbox import TuiMailbox
from .app import run_tui
from .app import prepend_initial_prompt
from .app import tui_is_supported
from .model import TuiEventSink
from .model import TuiProjector
from .model import TuiState
from .worker import TuiInteractionBridge
from .worker import TuiRuntimePort
from .worker import TuiWorker

__all__ = [
    "TuiEventSink",
    "TuiInteractionBridge",
    "TuiMailbox",
    "TuiProjector",
    "TuiRuntimePort",
    "TuiState",
    "TuiWorker",
    "run_tui",
    "prepend_initial_prompt",
    "tui_is_supported",
]
