"""Runtime adapters (§6). One package per runtime; adding a third runtime
means a new adapter class implementing `AgentAdapter` and a row in
`agents` (§6.4) — nothing in the core changes."""

from .base import AdapterHealth, AgentAdapter

__all__ = ["AdapterHealth", "AgentAdapter"]
