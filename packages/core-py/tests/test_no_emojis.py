"""Master §60: NO EMOJIS. Every text the operator sees comes from the
dashboard code (apps/web) or from Python (API responses, Telegram alerts,
reports); none of it may contain an emoji. Icons are lucide components."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⭐⭕️]")  # pictographs, symbols, dingbats, VS-16
SOURCES = [("apps/web/app", "*.tsx"), ("apps/web/components", "*.tsx"), ("apps/web/lib", "*.ts"),
           ("packages/core-py/yonixalpha_core", "*.py"), ("apps/api/app", "*.py"), ("services", "app/**/*.py")]


def test_no_emoji_in_anything_the_operator_sees():
    if not (ROOT / "apps" / "web").exists():
        pytest.skip("not run from a full checkout")
    scanned, hits = 0, []
    for base, pattern in SOURCES:
        for f in (ROOT / base).rglob(pattern) if "**" not in pattern else (ROOT / base).glob("*/" + pattern):
            if "node_modules" in f.parts or ".next" in f.parts:
                continue
            scanned += 1
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if EMOJI.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert scanned > 300, scanned  # the scan really covered the code
    assert hits == []
