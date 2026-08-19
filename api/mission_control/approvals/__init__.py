"""The approvals subsystem — the security boundary of the whole system (§7).

Built second, tested hardest, and kept free of feature bleed. Everything
here fails closed: on error, on timeout, on ambiguity, on an unparseable
payload, the answer is "not approved".
"""

from .classifier import RiskAssessment, classify
from .policy import PolicyDecision, evaluate
from .service import ApprovalService, DecisionOutcome, IdempotencyConflict, RateLimited

__all__ = [
    "RiskAssessment",
    "classify",
    "PolicyDecision",
    "evaluate",
    "ApprovalService",
    "DecisionOutcome",
    "IdempotencyConflict",
    "RateLimited",
]
