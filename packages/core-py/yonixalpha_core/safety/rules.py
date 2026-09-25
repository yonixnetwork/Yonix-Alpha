import fnmatch
import operator
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from yonixalpha_core.safety.models import FinalDecision

# Scopes: GLOBAL, the Pump.fun lifecycles (FRESH = bonding curve, which
# covers the fresh and momentum engines; MIGRATED = PumpSwap), or one engine.
BLACKLIST_SCOPES = ("GLOBAL", "FRESH", "MIGRATED", "solana_fresh", "solana_migration", "solana_momentum")
BLACKLIST_FIELDS = ("name", "symbol", "mint", "metadata", "any")
BLACKLIST_MATCH = ("exact", "word", "substring", "pattern", "regex")
BLACKLIST_ACTIONS = ("BLOCK", "ALLOW")
LIFECYCLE_OF_ENGINE = {"solana_fresh": "FRESH", "solana_momentum": "FRESH", "solana_migration": "MIGRATED"}
MAX_MATCH_INPUT = 200  # bounded input keeps even a pathological regex fast
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")

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
    action: str = "BLOCK"


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


def validate_blacklist_rule(scope: str, field: str, value: str, match_type: str, action: str = "BLOCK") -> list[str]:
    errors = []
    if scope not in BLACKLIST_SCOPES:
        errors.append(f"scope must be one of {BLACKLIST_SCOPES}")
    if field not in BLACKLIST_FIELDS:
        errors.append(f"field must be one of {BLACKLIST_FIELDS}")
    if match_type not in BLACKLIST_MATCH:
        errors.append(f"match_type must be one of {BLACKLIST_MATCH}")
    if action not in BLACKLIST_ACTIONS:
        errors.append(f"action must be one of {BLACKLIST_ACTIONS}")
    if not value or not value.strip():
        errors.append("value must not be empty")
    elif len(value) > MAX_PATTERN_LENGTH:
        errors.append(f"value must be at most {MAX_PATTERN_LENGTH} characters")
    if match_type == "pattern" and value and value.strip("*?") == "":
        errors.append("a pattern of only wildcards would match every asset")
    if match_type == "regex" and value:
        if _NESTED_QUANTIFIER.search(value) or re.search(r"\\[1-9]", value):
            errors.append("regex with nested quantifiers or back-references is refused (catastrophic backtracking)")
        else:
            try:
                rx = re.compile(value, re.IGNORECASE)
            except re.error as exc:
                errors.append(f"invalid regex: {exc}")
            else:
                if rx.search("") is not None:
                    errors.append("a regex that matches the empty string would match every asset")
    if match_type == "substring" and value and len(value.strip()) < 3:
        errors.append("substring rules need at least 3 characters (shorter ones match unrelated words)")
    return errors


def rule_matches(rule: BlacklistRule, text: str) -> bool:
    """Case-insensitive match of one rule against one value.
    - exact: the whole value equals the rule;
    - word: the rule appears as a whole word (letters/digits on either side
      don't count), so "rug" matches "RUG PULL" but not "drug" or "rugby";
    - substring: anywhere, deliberately broad;
    - pattern: shell glob (* ?);
    - regex: Python regex on input capped at 200 characters."""
    a = text.casefold()[:MAX_MATCH_INPUT]
    b = rule.value.casefold().strip()
    if rule.match_type == "exact":
        return a.strip() == b
    if rule.match_type == "word":
        return re.search(r"(?<![0-9a-z])" + re.escape(b) + r"(?![0-9a-z])", a) is not None
    if rule.match_type == "substring":
        return b in a
    if rule.match_type == "pattern":
        return fnmatch.fnmatchcase(a, b)
    if rule.match_type == "regex":
        try:
            return re.search(rule.value, text[:MAX_MATCH_INPUT], re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def match_blacklist(rules: list[BlacklistRule], engine: str, name: str | None, symbol: str | None, mint: str,
                    metadata: str | None = None) -> str | None:
    """Returns a description of the first matching BLOCK rule not waived by
    a matching ALLOW rule in scope, or None. ALLOW rules only waive
    word-filter blocks; they never lift any safety check."""
    lifecycle = LIFECYCLE_OF_ENGINE.get(engine)
    in_scope = [r for r in rules if r.enabled and r.scope in ("GLOBAL", engine, lifecycle)]
    values = {"name": name, "symbol": symbol, "mint": mint, "metadata": metadata}

    def texts(rule: BlacklistRule) -> list[str]:
        if rule.field == "any":
            return [v for k, v in values.items() if v and k != "mint"]
        v = values.get(rule.field)
        return [v] if v else []

    allows = [r for r in in_scope if r.action == "ALLOW"]
    for rule in in_scope:
        if rule.action != "BLOCK":
            continue
        for text in texts(rule):
            if rule_matches(rule, text) and not any(rule_matches(a, text) for a in allows if a.field in (rule.field, "any")):
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
