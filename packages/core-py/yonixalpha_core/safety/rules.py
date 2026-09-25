import fnmatch
import operator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from yonixalpha_core.safety.models import FinalDecision

BLACKLIST_SCOPES = ("GLOBAL", "solana_fresh", "solana_migration", "solana_momentum")
BLACKLIST_FIELDS = ("name", "symbol", "mint")
BLACKLIST_MATCH = ("exact", "pattern")

RULE_OPERATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
RULE_ACTIONS = {
    "ALLOW": FinalDecision.EXECUTE,
    "REJECT": FinalDecision.REJECT,
    "WAIT": FinalDecision.WAIT,
    "REQUIRE_MANUAL_APPROVAL": FinalDecision.REQUIRE_MANUAL_APPROVAL,
}
MAX_PATTERN_LENGTH = 100


@dataclass(frozen=True)
class BlacklistRule:
    id: str
    scope: str
    field: str
    value: str
    match_type: str
    enabled: bool = True


@dataclass(frozen=True)
class CustomRule:
    id: str
    name: str
    field: str
    op: str
    threshold: str
    action: str
    scope: str
    enabled: bool = True


def validate_blacklist_rule(scope: str, field: str, value: str, match_type: str) -> list[str]:
    errors = []
    if scope not in BLACKLIST_SCOPES:
        errors.append(f"scope must be one of {BLACKLIST_SCOPES}")
    if field not in BLACKLIST_FIELDS:
        errors.append(f"field must be one of {BLACKLIST_FIELDS}")
    if match_type not in BLACKLIST_MATCH:
        errors.append(f"match_type must be one of {BLACKLIST_MATCH}")
    if not value or not value.strip():
        errors.append("value must not be empty")
    elif len(value) > MAX_PATTERN_LENGTH:
        errors.append(f"value must be at most {MAX_PATTERN_LENGTH} characters")
    if match_type == "pattern" and value and value.strip("*?") == "":
        errors.append("a pattern of only wildcards would match every asset")
    return errors


def match_blacklist(rules: list[BlacklistRule], engine: str, name: str | None, symbol: str | None, mint: str) -> str | None:
    """Returns a description of the first matching enabled rule, or None.
    Case-insensitive; patterns are shell-style globs (fnmatch), deliberately
    not regular expressions — an operator-entered regex can backtrack
    catastrophically, a glob can't."""
    values = {"name": name, "symbol": symbol, "mint": mint}
    for rule in rules:
        if not rule.enabled or rule.scope not in ("GLOBAL", engine):
            continue
        candidate = values.get(rule.field)
        if not candidate:
            continue
        a, b = candidate.casefold(), rule.value.casefold()
        hit = fnmatch.fnmatchcase(a, b) if rule.match_type == "pattern" else a == b
        if hit:
            return f"{rule.scope} {rule.field} {rule.match_type} '{rule.value}'"
    return None


def validate_custom_rule(field: str, op: str, threshold: str, action: str, allowed_fields: set[str]) -> list[str]:
    errors = []
    if field not in allowed_fields:
        errors.append(f"field must be one of {sorted(allowed_fields)}")
    if op not in RULE_OPERATORS:
        errors.append(f"operator must be one of {list(RULE_OPERATORS)}")
    if action not in RULE_ACTIONS:
        errors.append(f"action must be one of {list(RULE_ACTIONS)}")
    try:
        Decimal(threshold)
    except (InvalidOperation, TypeError):
        errors.append("threshold must be numeric")
    return errors


def evaluate_custom_rules(rules: list[CustomRule], engine: str, features: dict[str, Any]) -> list[tuple[str, FinalDecision]]:
    """Returns (rule name, action) for every enabled rule whose condition
    holds. A rule whose field is missing from `features` does NOT fire —
    but it's reported as unevaluable so the caller can surface that rather
    than silently treating it as passed."""
    fired: list[tuple[str, FinalDecision]] = []
    for rule in rules:
        if not rule.enabled or rule.scope not in ("GLOBAL", engine):
            continue
        raw = features.get(rule.field)
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
            threshold = Decimal(rule.threshold)
        except InvalidOperation:
            continue
        if RULE_OPERATORS[rule.op](value, threshold):
            fired.append((rule.name, RULE_ACTIONS[rule.action]))
    return fired
