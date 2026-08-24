"""Read-only CodeAct execution for CPTR.

This package is an execution strategy, not a model or authority layer.
It is intentionally disabled by default and only accepts capabilities supplied
by the authenticated CPTR/FlowDeck caller.
"""

from cptr.codeact.contracts import (
    CodeActConfig,
    CodeActIdentity,
    CodeActLimits,
    CodeActMode,
    CodeActResult,
    CapabilityCall,
)
from cptr.codeact.repl import CodeActCapabilityError, CodeActRepl, ReadOnlyCapabilitySDK
from cptr.codeact.runner import run_read_only_attempt

__all__ = [
    "CapabilityCall",
    "CodeActCapabilityError",
    "CodeActConfig",
    "CodeActIdentity",
    "CodeActLimits",
    "CodeActMode",
    "CodeActRepl",
    "CodeActResult",
    "ReadOnlyCapabilitySDK",
    "run_read_only_attempt",
]