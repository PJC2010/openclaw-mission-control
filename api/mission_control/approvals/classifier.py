"""Server-side risk classification (§7.4).

The agent may be compromised (§12 threat model), so its self-declared risk
is stored separately as `claimed_risk` and never consulted here. This
module looks only at the tool name, the complete arguments, and the
configured workspace allowlist.

Design posture — this classifier is a FLOOR, not an oracle. A determined
attacker can obfuscate a payload past any pattern set. What the floor
guarantees is that the obvious, high-consequence categories in §7.4 are
always routed to a human and can never be auto-approved (S8), and that
anything we fail to understand lands in a *more* restrictive bucket, never
a less restrictive one. Every ambiguity resolves upward.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..models.enums import RiskLevel

# Categories from §7.4. Any match makes the request `critical`, which is
# never auto-approvable regardless of policy or objective (S8).
CATEGORY_CREDENTIALS = "credentials"
CATEGORY_FINANCIAL = "financial"
CATEGORY_EXTERNAL_COMMS = "external_comms"
CATEGORY_DESTRUCTIVE_FS = "destructive_fs"
CATEGORY_SELF_MODIFICATION = "self_modification"
CATEGORY_PRIVILEGE = "privilege"
CATEGORY_WORKSPACE_ESCAPE = "workspace_escape"

CRITICAL_CATEGORIES = frozenset(
    {
        CATEGORY_CREDENTIALS,
        CATEGORY_FINANCIAL,
        CATEGORY_EXTERNAL_COMMS,
        CATEGORY_DESTRUCTIVE_FS,
        CATEGORY_SELF_MODIFICATION,
        CATEGORY_PRIVILEGE,
        CATEGORY_WORKSPACE_ESCAPE,
    }
)

# Zero-width and bidi controls: pure obfuscation vectors in this context.
_INVISIBLE = dict.fromkeys(
    [
        0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2028, 0x2029,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x2066, 0x2067,
        0x2068, 0x2069, 0xFEFF,
    ]
)


def normalize(text: str) -> str:
    """Fold away the cheap evasions before matching.

    NFKC collapses fullwidth/compatibility forms, invisible controls are
    dropped outright, and case is folded. This does not stop a determined
    encoder — it stops the trivial ones from silently downgrading risk.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_INVISIBLE)
    return text.casefold()


def tokenize_name(name: str) -> str:
    """Normalize a tool name into space-separated words.

    Tool names are overwhelmingly snake/kebab/dotted (`read_file`,
    `execute-shell`, `fs.writeFile`). Word-boundary patterns do not fire
    across `_`, so without this an `execute_shell` tool would grade below
    `bash` — the separators must become spaces before matching. camelCase
    is split too, so `writeFile` reads as `write file`.
    """
    spaced = re.sub(r"[_\-./:]+", " ", name or "")
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    return normalize(spaced)


def _walk_strings(value: Any, depth: int = 0) -> Iterable[str]:
    """Every string anywhere in the argument structure, keys included.

    Keys matter: {"aws_secret_access_key": "..."} is a credential handling
    call even when the value is opaque.
    """
    if depth > 12:  # cycle/pathological-nesting guard
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _walk_strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _walk_strings(item, depth + 1)
    elif isinstance(value, (int, float, bool)) or value is None:
        return
    else:
        yield str(value)


@dataclass(frozen=True)
class Rule:
    category: str
    pattern: re.Pattern[str]
    why: str


def _rule(category: str, pattern: str, why: str) -> Rule:
    return Rule(category, re.compile(pattern, re.IGNORECASE | re.DOTALL), why)


# ── §7.4 rule set ────────────────────────────────────────────────────────
# Written against normalized (NFKC + casefolded) text.

RULES: tuple[Rule, ...] = (
    # "anything reading or writing credentials, tokens, key files, .env"
    _rule(CATEGORY_CREDENTIALS, r"(^|[\s\"'/=:,])\.env(\.[a-z0-9_-]+)?($|[\s\"'/,;)])", "references a .env file"),
    _rule(CATEGORY_CREDENTIALS, r"\bid_(rsa|dsa|ecdsa|ed25519)\b", "references an SSH private key"),
    _rule(CATEGORY_CREDENTIALS, r"\.(pem|key|pfx|p12|jks|keystore)\b", "references a key/certificate file"),
    _rule(CATEGORY_CREDENTIALS, r"(authorized_keys|known_hosts|\.ssh/|/\.ssh\b)", "touches SSH configuration"),
    _rule(CATEGORY_CREDENTIALS, r"(\.aws/credentials|\.aws/config|\.kube/config|\.docker/config\.json|\.netrc|\.npmrc|\.pypirc|\.git-credentials)", "touches a stored-credential file"),
    _rule(CATEGORY_CREDENTIALS, r"(/etc/shadow|/etc/passwd|/etc/sudoers)", "touches a system account file"),
    _rule(CATEGORY_CREDENTIALS, r"\b(secret|credential|passphrase|private[_\- ]?key)s?\b", "mentions secrets/credentials"),
    _rule(CATEGORY_CREDENTIALS, r"\b(api[_\- ]?key|access[_\- ]?token|refresh[_\- ]?token|auth[_\- ]?token|bearer[_\- ]?token|client[_\- ]?secret|session[_\- ]?token)s?\b", "mentions an API key or token"),
    _rule(CATEGORY_CREDENTIALS, r"\b(password|passwd|pwd)\b", "mentions a password"),
    _rule(CATEGORY_CREDENTIALS, r"\b(printenv|os\.environ|process\.env|getenv)\b", "reads process environment"),
    _rule(CATEGORY_CREDENTIALS, r"\b(keychain|security\s+find-(generic|internet)-password|gnome-keyring|secret-tool|pass\s+show)\b", "reads an OS keychain"),
    _rule(CATEGORY_CREDENTIALS, r"\b(gh\s+auth\s+token|aws\s+configure|op\s+(read|item)|vault\s+(read|kv))\b", "reads a credential store"),

    # "payments or financial APIs"
    _rule(CATEGORY_FINANCIAL, r"\b(stripe|paypal|braintree|adyen|squareup|plaid|dwolla|wise\.com|venmo|cash\.app)\b", "calls a payment provider"),
    _rule(CATEGORY_FINANCIAL, r"\b(coinbase|binance|kraken|metamask|walletconnect)\b", "calls a crypto exchange/wallet"),
    _rule(CATEGORY_FINANCIAL, r"/v1/(charges|payment_intents|payouts|transfers|subscriptions)\b", "hits a payments API path"),
    _rule(CATEGORY_FINANCIAL, r"\b(payment|payout|refund|chargeback|wire[_\- ]?transfer|bank[_\- ]?transfer|ach[_\- ]?transfer)s?\b", "performs a money movement"),
    _rule(CATEGORY_FINANCIAL, r"\b(credit[_\- ]?card|card[_\- ]?number|iban|routing[_\- ]?number|account[_\- ]?number|cvv|sort[_\- ]?code)\b", "handles payment instrument data"),

    # "outbound email, SMS, or posting to external accounts"
    # NOTE: this is about TOOL CALLS that reach external accounts. A runtime
    # replying on its own connected channel is ordinary operation and does
    # not traverse the approval path.
    _rule(CATEGORY_EXTERNAL_COMMS, r"\b(sendmail|smtplib|smtp\.|mailgun|sendgrid|postmark|mailchimp|resend\.com|amazonses|ses\.send)\b", "sends outbound email"),
    _rule(CATEGORY_EXTERNAL_COMMS, r"\b(send[_\- ]?(e?mail|sms|text|message|dm)|mail\.send|messages\.create)\b", "sends an outbound message"),
    _rule(CATEGORY_EXTERNAL_COMMS, r"\b(twilio|messagebird|vonage|nexmo|plivo)\b", "sends SMS/voice via a gateway"),
    _rule(CATEGORY_EXTERNAL_COMMS, r"\b(tweet|api\.x\.com|twitter\.com/i/api|linkedin\.com/(voyager|api)|graph\.facebook\.com|instagram\.com/api|reddit\.com/api/submit)\b", "posts to an external social account"),
    _rule(CATEGORY_EXTERNAL_COMMS, r"\b(webhooks?/|chat\.postmessage|discord(app)?\.com/api/webhooks|hooks\.slack\.com)\b", "posts to an external webhook"),
    _rule(CATEGORY_EXTERNAL_COMMS, r"\bgit\s+push\b", "publishes code to a remote"),

    # "destructive filesystem operations"
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]", "recursive/forced delete"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\b(shred|wipefs|mkfs(\.[a-z0-9]+)?|fdisk|parted|diskutil\s+erase)\b", "destroys a filesystem/disk"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\bdd\s+.*\bof=/dev/", "raw writes to a block device"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r">\s*/dev/(sd|nvme|disk|hd)", "redirects onto a block device"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\bfind\b.*\s-(delete|exec\s+rm)\b", "bulk delete via find"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+(--force|-f)\b)", "destroys git history/worktree"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\bdrop\s+(table|database|schema)\b", "drops a database object"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\btruncate\s+table\b", "truncates a table"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\bdelete\s+from\b(?!.*\bwhere\b)", "unbounded SQL delete"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\bchmod\s+(-[a-z]*r[a-z]*\s+)?(777|a\+rwx)", "world-writable permissions"),
    _rule(CATEGORY_DESTRUCTIVE_FS, r"\b(chown|chmod)\s+-[a-z]*r[a-z]*\s+", "recursive ownership/permission change"),

    # "package installation, or modification of the agent's own skills, config, or memory"
    _rule(CATEGORY_SELF_MODIFICATION, r"\b(pip3?|uv)\s+(pip\s+)?(install|add)\b", "installs a Python package"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|link)\b", "installs a JS package"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\b(apt|apt-get|dnf|yum|pacman|apk|brew|port)\s+(install|add|-s\s+install)\b", "installs a system package"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\b(cargo|gem|go|composer|nuget)\s+install\b", "installs a package"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b", "pipes a remote script into a shell"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\bwget\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b", "pipes a remote script into a shell"),
    _rule(CATEGORY_SELF_MODIFICATION, r"(\.hermes/|\.openclaw/|/\.claude/|openclaw\.json|cli-config\.ya?ml)", "modifies agent configuration"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\b(skill\.md|skills?/[a-z0-9_-]+/|plugins\.(install|setenabled|uninstall))\b", "modifies agent skills/plugins"),
    _rule(CATEGORY_SELF_MODIFICATION, r"\b(state\.db|memory\.(db|json)|jobs\.json|executions\.db)\b", "modifies agent memory/schedule state"),

    # "sudo, systemd unit changes, firewall or Tailscale config changes"
    _rule(CATEGORY_PRIVILEGE, r"(^|[\s;&|(])(sudo|doas|pkexec)\s", "runs with elevated privilege"),
    _rule(CATEGORY_PRIVILEGE, r"(^|[\s;&|(])su\s+(-|[a-z])", "switches user"),
    _rule(CATEGORY_PRIVILEGE, r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|edit)\b", "changes a systemd unit"),
    _rule(CATEGORY_PRIVILEGE, r"(/etc/systemd/|\.service\b.*\b(write|create|install))", "writes a systemd unit"),
    _rule(CATEGORY_PRIVILEGE, r"\b(iptables|ip6tables|nft|ufw|firewall-cmd|pfctl)\b", "changes firewall rules"),
    _rule(CATEGORY_PRIVILEGE, r"\btailscale\s+(up|down|set|serve|funnel|logout|login|cert)\b", "changes Tailscale configuration"),
    _rule(CATEGORY_PRIVILEGE, r"\b(crontab|/etc/cron)", "changes scheduled system jobs"),
    _rule(CATEGORY_PRIVILEGE, r"\b(useradd|usermod|userdel|groupadd|visudo|passwd)\b", "changes system accounts"),
    _rule(CATEGORY_PRIVILEGE, r"\bdocker\s+.*(--privileged|-v\s*/:|--net(work)?=host)", "runs a privileged container"),
    _rule(CATEGORY_PRIVILEGE, r"\b(setcap|chattr\s+[+-]i|mount\s|umount\s)", "changes kernel/filesystem attributes"),
)

# Tools whose *name alone* implies shell/code execution: without a matching
# critical rule they still land at `high`, never below.
EXEC_TOOL_PATTERN = re.compile(
    r"\b(bash|sh|shell|exec|execute|run[_\-]?(command|script|shell)?|terminal|system|"
    r"subprocess|process|command|eval|python|node|script|ssh|docker)\b",
    re.IGNORECASE,
)
WRITE_TOOL_PATTERN = re.compile(
    r"\b(write|create|edit|patch|apply|save|upload|put|post|delete|remove|move|rename|"
    r"mkdir|chmod|chown|append|replace|insert|update)\b",
    re.IGNORECASE,
)
READ_TOOL_PATTERN = re.compile(
    r"\b(read|get|list|search|find|grep|glob|fetch|view|show|describe|inspect|status|cat|head|tail)\b",
    re.IGNORECASE,
)

PATH_PATTERN = re.compile(r"(?:^|[\s\"'=,:(\[])((?:~|/|\.\.?/)[^\s\"',;)\]]{1,4096})")


@dataclass
class RiskAssessment:
    level: RiskLevel
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #  True when a §7.4 category matched — S8: never auto-approvable.
    never_auto_approvable: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "categories": list(self.categories),
            "reasons": list(self.reasons),
            "never_auto_approvable": self.never_auto_approvable,
        }


def _outside_workspace(paths: Sequence[str], allowlist: Sequence[str]) -> list[str]:
    """Paths that escape every configured workspace root.

    An empty allowlist means no workspace is configured, so we cannot prove
    containment for anything — and per §7.4 an unprovable write is treated
    as an escape rather than waved through.
    """
    import posixpath

    escapes: list[str] = []
    roots = [posixpath.normpath(root).rstrip("/") or "/" for root in allowlist if root.strip()]
    for raw in paths:
        candidate = raw.strip().strip("\"'")
        if candidate.startswith("~"):
            escapes.append(candidate)  # home-relative: outside any workspace root
            continue
        if not candidate.startswith("/"):
            # Relative path: only safe if it cannot climb out.
            if ".." in candidate.split("/"):
                escapes.append(candidate)
            continue
        normalized = posixpath.normpath(candidate)
        if not roots:
            escapes.append(candidate)
            continue
        if not any(normalized == root or normalized.startswith(root + "/") for root in roots):
            escapes.append(candidate)
    return escapes


def classify(
    tool_name: str,
    tool_args: Any,
    workspace_allowlist: Sequence[str] = (),
    known_tool: bool = False,
) -> RiskAssessment:
    """Classify a gated tool call. Ambiguity always resolves upward."""
    raw_parts: list[str] = [tool_name or ""]
    try:
        raw_parts.extend(_walk_strings(tool_args))
    except Exception:  # noqa: BLE001 — unwalkable args are suspicious, not benign
        return RiskAssessment(
            level=RiskLevel.CRITICAL,
            categories=[CATEGORY_WORKSPACE_ESCAPE],
            reasons=["arguments could not be parsed for inspection"],
            never_auto_approvable=True,
        )

    corpus = normalize("\n".join(part for part in raw_parts if part))
    normalized_tool = tokenize_name(tool_name)

    categories: list[str] = []
    reasons: list[str] = []
    for rule in RULES:
        if rule.pattern.search(corpus):
            if rule.category not in categories:
                categories.append(rule.category)
            if rule.why not in reasons:
                reasons.append(rule.why)

    # Writes outside the workspace allowlist are their own §7.4 category.
    looks_write = bool(WRITE_TOOL_PATTERN.search(normalized_tool)) or bool(
        EXEC_TOOL_PATTERN.search(normalized_tool)
    )
    if looks_write:
        paths = [match.group(1) for match in PATH_PATTERN.finditer(corpus)]
        escapes = _outside_workspace(paths, workspace_allowlist)
        if escapes:
            if CATEGORY_WORKSPACE_ESCAPE not in categories:
                categories.append(CATEGORY_WORKSPACE_ESCAPE)
            sample = ", ".join(sorted(set(escapes))[:3])
            reasons.append(f"writes outside the workspace allowlist ({sample})")

    if categories:
        return RiskAssessment(
            level=RiskLevel.CRITICAL,
            categories=categories,
            reasons=reasons,
            never_auto_approvable=True,
        )

    # No critical category. Grade the remainder conservatively.
    if EXEC_TOOL_PATTERN.search(normalized_tool):
        return RiskAssessment(RiskLevel.HIGH, [], ["executes commands or code"])
    if not known_tool:
        # §7.6: the default for an unknown tool is gated.
        return RiskAssessment(RiskLevel.HIGH, [], ["unrecognized tool — gated by default"])
    if WRITE_TOOL_PATTERN.search(normalized_tool):
        return RiskAssessment(RiskLevel.MEDIUM, [], ["modifies state"])
    if READ_TOOL_PATTERN.search(normalized_tool):
        return RiskAssessment(RiskLevel.LOW, [], ["read-only operation"])
    return RiskAssessment(RiskLevel.MEDIUM, [], ["unclassified operation"])
