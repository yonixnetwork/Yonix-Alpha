from yonixalpha_core.solana.token_program import (
    extract_mint_creations,
    extract_transfer_checked,
    logs_mention_mint_init,
    logs_mention_transfer_checked,
)


def test_logs_mention_mint_init_true():
    assert logs_mention_mint_init(["Program log: Instruction: InitializeMint2", "Program consumed 1400 units"])


def test_logs_mention_mint_init_false_for_unrelated_logs():
    assert not logs_mention_mint_init(["Program log: Instruction: Transfer", "Program consumed 200 units"])


def test_logs_mention_mint_init_empty_list():
    assert not logs_mention_mint_init([])


def test_logs_mention_transfer_checked_true():
    assert logs_mention_transfer_checked(["Program log: Instruction: TransferChecked"])


def test_logs_mention_transfer_checked_false_for_plain_transfer():
    assert not logs_mention_transfer_checked(["Program log: Instruction: Transfer"])


def _jsonparsed_tx(instructions, inner_instructions=None, fee_payer="FeePayer11111111111111111111111111111111"):
    return {
        "blockTime": 1700000000,
        "transaction": {
            "signatures": ["Sig1111111111111111111111111111111111111111111111111111111111111111111"],
            "message": {
                "accountKeys": [
                    {"pubkey": fee_payer, "signer": True, "writable": True},
                    {"pubkey": "OtherAccount1111111111111111111111111111", "signer": False, "writable": True},
                ],
                "instructions": instructions,
            },
        },
        "meta": {"innerInstructions": inner_instructions or []},
    }


def _init_mint_instruction(mint: str, mint_authority: str = "Authority1111111111111111111111111111111", decimals: int = 9):
    return {
        "program": "spl-token",
        "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "parsed": {
            "type": "initializeMint2",
            "info": {"mint": mint, "mintAuthority": mint_authority, "decimals": decimals},
        },
    }


def _transfer_checked_instruction(
    mint: str,
    amount: str = "1000000",
    decimals: int = 6,
    source: str = "SourceAcct111111111111111111111111111111",
    destination: str = "DestAcct1111111111111111111111111111111",
    authority: str = "Authority1111111111111111111111111111111",
):
    return {
        "program": "spl-token",
        "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "parsed": {
            "type": "transferChecked",
            "info": {
                "mint": mint,
                "source": source,
                "destination": destination,
                "authority": authority,
                "tokenAmount": {"amount": amount, "decimals": decimals},
            },
        },
    }


def test_extract_mint_creations_top_level_instruction():
    tx = _jsonparsed_tx([_init_mint_instruction("Mint1111111111111111111111111111111111111")])
    results = extract_mint_creations(tx)

    assert len(results) == 1
    assert results[0]["mint"] == "Mint1111111111111111111111111111111111111"
    assert results[0]["mint_authority"] == "Authority1111111111111111111111111111111"
    assert results[0]["decimals"] == 9
    assert results[0]["fee_payer"] == "FeePayer11111111111111111111111111111111"
    assert results[0]["block_time"] == 1700000000
    assert results[0]["signature"].startswith("Sig1")


def test_extract_mint_creations_inner_instruction_cpi():
    tx = _jsonparsed_tx(
        instructions=[{"program": "system", "programId": "11111111111111111111111111111111111111111", "parsed": {"type": "createAccount", "info": {}}}],
        inner_instructions=[{"index": 0, "instructions": [_init_mint_instruction("CpiMint111111111111111111111111111111111")]}],
    )
    results = extract_mint_creations(tx)

    assert len(results) == 1
    assert results[0]["mint"] == "CpiMint111111111111111111111111111111111"


def test_extract_mint_creations_ignores_unrelated_instructions():
    tx = _jsonparsed_tx(
        [
            {"program": "system", "programId": "111...", "parsed": {"type": "transfer", "info": {}}},
            {"program": "spl-token", "programId": "Token...", "parsed": {"type": "transfer", "info": {"amount": "100"}}},
        ]
    )
    assert extract_mint_creations(tx) == []


def test_extract_mint_creations_handles_missing_parsed_field_gracefully():
    tx = _jsonparsed_tx([{"programId": "SomeUnknownProgram11111111111111111111111", "data": "abc123", "accounts": []}])
    assert extract_mint_creations(tx) == []


def test_extract_mint_creations_empty_transaction_result():
    assert extract_mint_creations({}) == []


def test_extract_mint_creations_malformed_structure_does_not_raise():
    assert extract_mint_creations({"transaction": "not-a-dict"}) == []
    assert extract_mint_creations({"transaction": {"message": None}}) == []


def test_extract_mint_creations_multiple_mints_in_one_transaction():
    tx = _jsonparsed_tx(
        [
            _init_mint_instruction("MintA111111111111111111111111111111111111"),
            _init_mint_instruction("MintB111111111111111111111111111111111111"),
        ]
    )
    results = extract_mint_creations(tx)
    assert {r["mint"] for r in results} == {"MintA111111111111111111111111111111111111", "MintB111111111111111111111111111111111111"}


def test_extract_transfer_checked_top_level():
    tx = _jsonparsed_tx([_transfer_checked_instruction("Mint1111111111111111111111111111111111111", amount="5000000", decimals=6)])
    results = extract_transfer_checked(tx)

    assert len(results) == 1
    assert results[0]["mint"] == "Mint1111111111111111111111111111111111111"
    assert results[0]["amount"] == "5000000"
    assert results[0]["decimals"] == 6
    assert results[0]["source"] == "SourceAcct111111111111111111111111111111"
    assert results[0]["destination"] == "DestAcct1111111111111111111111111111111"


def test_extract_transfer_checked_ignores_plain_transfer():
    """Plain (non-Checked) Transfer instructions have no `mint` field in
    jsonParsed info and are deliberately not extracted — see
    logs_mention_transfer_checked's docstring.
    """
    tx = _jsonparsed_tx(
        [{"program": "spl-token", "programId": "Token...", "parsed": {"type": "transfer", "info": {"source": "A", "destination": "B", "amount": "100"}}}]
    )
    assert extract_transfer_checked(tx) == []


def test_extract_transfer_checked_ignores_mint_creation():
    tx = _jsonparsed_tx([_init_mint_instruction("Mint1111111111111111111111111111111111111")])
    assert extract_transfer_checked(tx) == []


def test_extract_transfer_checked_multiple_in_one_transaction():
    tx = _jsonparsed_tx(
        [
            _transfer_checked_instruction("MintA111111111111111111111111111111111111"),
            _transfer_checked_instruction("MintB111111111111111111111111111111111111"),
        ]
    )
    results = extract_transfer_checked(tx)
    assert {r["mint"] for r in results} == {"MintA111111111111111111111111111111111111", "MintB111111111111111111111111111111111111"}


def test_extract_transfer_checked_empty_transaction_result():
    assert extract_transfer_checked({}) == []
