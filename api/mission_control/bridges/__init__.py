"""Runtime-side approval bridges.

Where an adapter observes, a bridge *decides*: it carries an operator's
verdict back into the runtime that is blocking on it.
"""

from .openclaw_approvals import OpenClawApprovalBridge

__all__ = ["OpenClawApprovalBridge"]
