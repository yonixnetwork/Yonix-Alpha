import os
from collections.abc import Callable
from typing import Any

from yonixalpha_core.logging import get_logger

log = get_logger("engine-solana-migration.detect")

# Unlike the SPL Token Program (one universal interface every token uses,
# which is what makes engine-solana-discovery and engine-solana-momentum's
# generic parsing approach possible), there is no single "migration"
# program: pump.fun-style migrations land on a specific AMM (historically
# Raydium, sometimes others), and each AMM has its own, mutually
# incompatible pool-initialization instruction layout. Picking one and
# hand-writing a parser for it without being able to verify the program ID
# and instruction shape against current documentation (see
# ARCHITECTURE_AUDIT.md's network-access note) would mean fabricating
# trading-relevant parsing logic — the one thing the project spec is most
# explicit about never doing (section 53). So this module ships the
# dispatch mechanism, fully working and tested, with an empty parser
# registry: an operator who has verified a specific AMM's program ID and
# instruction format registers a parser for it (see register_parser below)
# and detection activates for that program with no other code changes.

PoolInitParser = Callable[[dict[str, Any]], dict[str, Any] | None]

_PARSERS: dict[str, PoolInitParser] = {}


def register_parser(program_id: str, parser: PoolInitParser) -> None:
    """Registers a jsonParsed-getTransaction parser for a specific AMM
    program. `parser` takes a getTransaction response and returns a dict
    describing the detected pool (at minimum: pool_address, token_mint,
    quote_mint, source_signature) or None if this specific transaction
    didn't contain a pool initialization after all.
    """
    _PARSERS[program_id] = parser


def configured_program_ids() -> dict[str, str]:
    """Parses MIGRATION_AMM_PROGRAM_IDS, e.g. "raydium_v4:675kPX9...,orca_whirlpool:whir..."
    Empty by default — see module docstring. Format is name:address pairs,
    comma-separated; malformed entries are skipped with a warning, never
    silently guessed at.
    """
    raw = os.getenv("MIGRATION_AMM_PROGRAM_IDS", "")
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            log.warning("detect.malformed_program_id_entry", entry=entry)
            continue
        name, _, address = entry.partition(":")
        result[address.strip()] = name.strip()
    return result


def has_parser(program_id: str) -> bool:
    return program_id in _PARSERS


def extract_pool_initialization(program_id: str, tx_result: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatches to a registered parser for `program_id`. Returns None —
    logging why — for any program with no registered parser, rather than
    raising: an operator can configure MIGRATION_AMM_PROGRAM_IDS to watch a
    program before a parser for it exists, and that must degrade to "not
    detecting anything from this program yet," never a crash.
    """
    parser = _PARSERS.get(program_id)
    if parser is None:
        log.warning("detect.no_parser_registered", program_id=program_id)
        return None
    return parser(tx_result)
