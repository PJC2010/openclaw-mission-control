"""Auto-approval policy evaluation (§5.7).

Allowlist semantics, and only allowlist semantics: a request auto-approves
*only* when an explicit, enabled `auto_approve` rule matches it. The
absence of any rule means ask. Every uncertainty — an unknown matcher
operator, an argument key that isn't present, a malformed rule — resolves
to "ask", never to "approve".

Two hard overrides sit above the rule table:
  * S8 — a `critical`-class request (§7.4) never auto-approves, whatever
    any policy or objective says.
  * A matching `always_ask` rule vetoes any matching `auto_approve` rule.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..models.enums import PolicyAction, RiskLevel
from .classifier import RiskAssessment, normalize

# Ordered weakest → strongest so `max_risk_level` comparisons are total.
RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

# Deliberately no regex operator: a policy is security configuration, and
# regex there invites catastrophic backtracking and subtle escape bugs.
SUPPORTED_OPERATORS = frozenset({"equals", "one_of", "prefix", "suffix", "contains"})


@dataclass(frozen=True)
class PolicyDecision:
    auto_approve: bool
    reason: str
    matched_policy_id: str | None = None


def _tool_matches(pattern: str, tool_name: str) -> bool:
    if not pattern:
        return False
    return fnmatch.fnmatchcase(normalize(tool_name), normalize(pattern))


def _lookup(args: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path against the argument structure.

    Returns (found, value). A missing key is (False, None), which fails the
    matcher — an absent argument must never satisfy an allowlist rule.
    """
    current = args
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, (list, tuple)) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _matcher_satisfied(matcher: Any, value: Any) -> bool:
    """One matcher against one resolved value. Unknown shapes → False."""
    # Bare scalar shorthand: {"path": "/workspace"} means equals.
    if not isinstance(matcher, dict):
        return value == matcher

    if not matcher:
        return False  # an empty matcher must not act as a wildcard
    for operator, expected in matcher.items():
        if operator not in SUPPORTED_OPERATORS:
            return False  # unknown operator: fail closed
        if operator == "equals":
            if value != expected:
                return False
        elif operator == "one_of":
            if not isinstance(expected, (list, tuple)) or value not in expected:
                return False
        elif operator in ("prefix", "suffix", "contains"):
            if not isinstance(value, str) or not isinstance(expected, str):
                return False
            haystack = normalize(value)
            needle = normalize(expected)
            if operator == "prefix" and not haystack.startswith(needle):
                return False
            if operator == "suffix" and not haystack.endswith(needle):
                return False
            if operator == "contains" and needle not in haystack:
                return False
    return True


def _args_match(arg_matchers: Any, tool_args: Any) -> bool:
    """Every matcher must be satisfied (AND). No matchers = no constraint."""
    if not arg_matchers:
        return True
    if not isinstance(arg_matchers, dict):
        return False
    for path, matcher in arg_matchers.items():
        if not isinstance(path, str):
            return False
        found, value = _lookup(tool_args, path)
        if not found or not _matcher_satisfied(matcher, value):
            return False
    return True


class PolicyLike:
    """Structural contract for a row from `approval_policies` (§5.7)."""

    id: Any
    objective_id: Any
    agent_id: Any
    tool_name_pattern: str
    arg_matchers: Any
    action: PolicyAction
    max_risk_level: RiskLevel
    enabled: bool


def _scope_matches(policy: PolicyLike, agent_id: Any, objective_id: Any) -> bool:
    if policy.agent_id is not None and policy.agent_id != agent_id:
        return False
    if policy.objective_id is not None and policy.objective_id != objective_id:
        return False
    return True


def evaluate(
    policies: Iterable[PolicyLike],
    *,
    tool_name: str,
    tool_args: Any,
    risk: RiskAssessment,
    agent_id: Any,
    objective_id: Any = None,
) -> PolicyDecision:
    """Decide whether this request may skip the human (§7.1 step 3)."""
    # S8 first: no policy can reach past a critical classification.
    if risk.never_auto_approvable or risk.level is RiskLevel.CRITICAL:
        return PolicyDecision(
            auto_approve=False,
            reason="critical-class action is never auto-approvable (§7.4/S8)",
        )

    candidates: list[PolicyLike] = []
    for policy in policies:
        if not policy.enabled:
            continue
        if not _scope_matches(policy, agent_id, objective_id):
            continue
        if not _tool_matches(policy.tool_name_pattern or "", tool_name):
            continue
        if not _args_match(policy.arg_matchers, tool_args):
            continue
        candidates.append(policy)

    # An explicit always_ask beats any auto_approve (most restrictive wins).
    for policy in candidates:
        if policy.action is PolicyAction.ALWAYS_ASK:
            return PolicyDecision(
                auto_approve=False,
                reason="an always_ask policy matched",
                matched_policy_id=str(policy.id),
            )

    request_rank = RISK_ORDER[risk.level]
    for policy in candidates:
        if policy.action is not PolicyAction.AUTO_APPROVE:
            continue
        ceiling = RISK_ORDER.get(policy.max_risk_level, RISK_ORDER[RiskLevel.LOW])
        if request_rank <= ceiling:
            return PolicyDecision(
                auto_approve=True,
                reason=f"auto_approve policy matched (risk {risk.level.value} <= {policy.max_risk_level.value})",
                matched_policy_id=str(policy.id),
            )

    return PolicyDecision(auto_approve=False, reason="no matching auto_approve policy — asking")
