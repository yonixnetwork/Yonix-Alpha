"""tools.fourmeme_modes analysis (pure): the word whose template bits agree
with the TaxToken (feeRate) and X Mode (plain-buy simulation) evidence on
every token stands out; the others do not. The real layout is NOT VERIFIED
until the tool runs on the server."""

from yonixalpha_core.tools.fourmeme_modes import X_MODE_BIT, analyse, words


def test_words_splits_32_byte_words():
    assert words("0x" + "00" * 31 + "05" + "00" * 31 + "07") == [5, 7]
    assert words(None) == [] and words("0x") == []


def test_analysis_finds_the_template_word():
    tax_template = 5 << 10
    rows = []
    for i in range(10):
        is_tax, is_x = i < 3, i in (5, 6)
        template = (tax_template if is_tax else 0) | (X_MODE_BIT if is_x else 0)
        rows.append({"token": f"t{i}", "plain_buy": "X_MODE" if is_x else "PLAIN_BUY_OK", "tax_bps": 100 if is_tax else 0,
                     "info_words": [123 + i, template, 0], "ex1_words": [0, 50 if i == 1 else 0],
                     "api_fee_plan": i == 1, "api_version": "V8" if is_x else "V2"})
    rows.append({"token": "u", "plain_buy": "NOT_SIMULATED", "tax_bps": None, "info_words": [], "ex1_words": []})
    out = "\n".join(analyse(rows))
    assert "plain-buy simulation: PLAIN_BUY_OK 8, X_MODE 2, NOT_SIMULATED 1" in out
    assert "word  1: TaxToken bits agree 10/10; X Mode bit agrees 10/10" in out
    assert "word  0: TaxToken bits agree 7/10; X Mode bit agrees 8/10" in out  # 123+i: (w>>10)&63 == 0, bit16 clear
    assert "word  1: non-zero 1/10; non-zero agrees with API feePlan 10/10" in out
    assert "agrees with version V8 on 10/10" in out
