"""Adapter supervisor — builds adapters from configuration, registers their
agents rows (§6.4), and owns their lifecycle."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .adapters import AgentAdapter
from .adapters.hermes import HermesAdapter
from .adapters.openclaw import OpenClawAdapter
from .bridges import OpenClawApprovalBridge
from .config import Settings
from .models.enums import AgentRuntime
from .normalizer import Normalizer

log = logging.getLogger("mission_control.runtime")


class AdapterSupervisor:
    def __init__(
        self, settings: Settings, normalizer: Normalizer, approvals: Any | None = None
    ) -> None:
        self._settings = settings
        self._normalizer = normalizer
        self._approvals = approvals
        self.adapters: dict[uuid.UUID, AgentAdapter] = {}
        self.bridges: dict[uuid.UUID, OpenClawApprovalBridge] = {}

    async def start(self) -> None:
        settings = self._settings
        if settings.openclaw_url:
            if not settings.openclaw_token:
                log.warning(
                    "openclaw adapter starting without a token — only valid if "
                    "gateway.auth.mode is not 'token'"
                )
            agent_id = await self._normalizer.register_agent(
                AgentRuntime.OPENCLAW,
                instance_key=f"openclaw:{settings.openclaw_url}",
                display_name=settings.openclaw_display_name,
            )
            bridge = None
            if self._approvals is not None:
                # The RPC route: the gateway raises approvals, we decide.
                bridge = OpenClawApprovalBridge(
                    agent_id, settings, self._approvals, self._normalizer
                )
                self.bridges[agent_id] = bridge
            self.adapters[agent_id] = OpenClawAdapter(
                agent_id, settings, self._normalizer, approval_bridge=bridge
            )
        if settings.hermes_home:
            agent_id = await self._normalizer.register_agent(
                AgentRuntime.HERMES,
                instance_key=f"hermes:{settings.hermes_home}",
                display_name=settings.hermes_display_name,
            )
            self.adapters[agent_id] = HermesAdapter(agent_id, settings, self._normalizer)
        for agent_id, adapter in self.adapters.items():
            await adapter.start()
            log.info("started %s adapter for agent %s", adapter.name, agent_id)
        if not self.adapters:
            log.warning(
                "no adapters configured (MC_OPENCLAW_URL / MC_HERMES_HOME empty) — "
                "observation is idle"
            )

    async def stop(self) -> None:
        for adapter in self.adapters.values():
            await adapter.stop()

    def health(self) -> dict[str, dict[str, object]]:
        report: dict[str, dict[str, object]] = {}
        for agent_id, adapter in self.adapters.items():
            entry: dict[str, object] = {"adapter": adapter.name, **adapter.health().as_json()}
            bridge = self.bridges.get(agent_id)
            if bridge is not None:
                entry["approval_bridge"] = {
                    "attached": bridge._client is not None,  # noqa: SLF001
                    "fail_open_risk": bridge.fail_open_risk,
                }
            report[str(agent_id)] = entry
        return report
