from typing import Any

from yonixalpha_core.logging import get_logger

log = get_logger("solana.token_program")

# The SPL Token Program's address is foundational, unchanged Solana
# infrastructure (not a specific launch platform's custom program), used
# here with high confidence. Everything else in this module — exact
# getTransaction jsonParsed response shape, instruction "type" strings —
# is implemented against the long-stable, widely-documented Solana JSON-RPC
# contract but has NOT been live-verified in this environment (see
# ARCHITECTURE_AUDIT.md's network-access note). Every parse step below is
# defensive (skip and log on unexpected shape, never raise past the caller)
# specifically because of that — a shape mismatch should degrade to "missed
# this one, keep going," not crash an ingestion loop.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

_MINT_INIT_TYPES = {"initializeMint", "initializeMint2"}
_TRANSFER_CHECKED_TYPES = {"transferChecked"}


def logs_mention_mint_init(logs: list[str]) -> bool:
    """Cheap pre-filter over raw log lines before spending an RPC call on
    getTransaction. Heuristic only — see module docstring; the authoritative
    check is extract_mint_creations()'s inspection of the parsed
    instruction type.
    """
    return any("InitializeMint" in line for line in logs)


def logs_mention_transfer_checked(logs: list[str]) -> bool:
    """Same idea as logs_mention_mint_init but for transferChecked. Plain
    (non-Checked) "Transfer" instructions are deliberately NOT matched or
    parsed anywhere in this module: unlike TransferChecked, a plain
    Transfer's jsonParsed info has no `mint` field (only source/destination
    token *accounts*), and resolving a token account to its mint would need
    an extra getAccountInfo call and a cache this module doesn't have. That
    is a disclosed gap, not a silent one — see engine-solana-momentum's
    README note.
    """
    return any("TransferChecked" in line for line in logs)


def _fee_payer(message: dict[str, Any]) -> str | None:
    account_keys = message.get("accountKeys", [])
    if not account_keys:
        return None
    first = account_keys[0]
    # jsonParsed accountKeys are {"pubkey", "signer", "writable", "source"}
    # objects; fall back to a plain string in case a caller hands us a
    # differently-encoded message.
    return first.get("pubkey") if isinstance(first, dict) else first


def _transaction_envelope(tx_result: dict[str, Any]) -> tuple[dict, str | None, int | None, str | None] | None:
    """Shared envelope extraction: (message, signature, block_time, fee_payer)."""
    try:
        transaction = tx_result.get("transaction", {})
        message = transaction.get("message", {})
        signature = (transaction.get("signatures") or [None])[0]
        block_time = tx_result.get("blockTime")
        fee_payer = _fee_payer(message)
        return message, signature, block_time, fee_payer
    except (AttributeError, TypeError) as exc:
        log.warning("token_program.malformed_transaction_envelope", error=str(exc))
        return None


def _scan_instructions(tx_result: dict[str, Any], message: dict, per_instruction) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def _scan(instructions: Any) -> None:
        if not isinstance(instructions, list):
            return
        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            try:
                extracted = per_instruction(instruction)
            except (AttributeError, TypeError) as exc:
                log.warning("token_program.malformed_instruction", error=str(exc))
                continue
            if extracted:
                results.append(extracted)

    _scan(message.get("instructions"))
    for inner in tx_result.get("meta", {}).get("innerInstructions", []) or []:
        _scan(inner.get("instructions"))

    return results


def _instruction_mint_creation(instruction: dict[str, Any]) -> dict[str, Any] | None:
    if instruction.get("program") != "spl-token":
        return None
    parsed = instruction.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") not in _MINT_INIT_TYPES:
        return None
    info = parsed.get("info", {})
    mint = info.get("mint")
    if not mint:
        return None
    return {"mint": mint, "mint_authority": info.get("mintAuthority"), "decimals": info.get("decimals")}


def extract_mint_creations(tx_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Given a getTransaction response (encoding=jsonParsed), returns one
    dict per InitializeMint/InitializeMint2 instruction found — checking
    both top-level instructions and inner instructions (a mint can be
    created via CPI, e.g. by a program that atomically creates and
    initializes it). Returns [] rather than raising on any structural
    surprise.
    """
    if not tx_result:
        return []
    envelope = _transaction_envelope(tx_result)
    if envelope is None:
        return []
    message, signature, block_time, fee_payer = envelope

    def per_instruction(instruction):
        creation = _instruction_mint_creation(instruction)
        if not creation:
            return None
        return {**creation, "signature": signature, "fee_payer": fee_payer, "block_time": block_time}

    return _scan_instructions(tx_result, message, per_instruction)


def _instruction_transfer_checked(instruction: dict[str, Any]) -> dict[str, Any] | None:
    if instruction.get("program") != "spl-token":
        return None
    parsed = instruction.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") not in _TRANSFER_CHECKED_TYPES:
        return None
    info = parsed.get("info", {})
    mint = info.get("mint")
    if not mint:
        return None
    token_amount = info.get("tokenAmount", {})
    return {
        "mint": mint,
        "amount": token_amount.get("amount"),
        "decimals": token_amount.get("decimals"),
        "source": info.get("source"),
        "destination": info.get("destination"),
        "authority": info.get("authority"),
    }


def extract_transfer_checked(tx_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Given a getTransaction response (encoding=jsonParsed), returns one
    dict per transferChecked instruction found (top-level and inner/CPI).
    Plain "Transfer" instructions are not extracted — see
    logs_mention_transfer_checked's docstring for why.
    """
    if not tx_result:
        return []
    envelope = _transaction_envelope(tx_result)
    if envelope is None:
        return []
    message, signature, block_time, fee_payer = envelope

    def per_instruction(instruction):
        transfer = _instruction_transfer_checked(instruction)
        if not transfer:
            return None
        return {**transfer, "signature": signature, "fee_payer": fee_payer, "block_time": block_time}

    return _scan_instructions(tx_result, message, per_instruction)
