# Alerting

YonixAlpha sends real-time Telegram alerts for the handful of events an
operator genuinely needs to know about the moment they happen, rather than
the next time they open the dashboard. This has been referenced in
`.env.example`/`config.py` since Phase 4 but never actually implemented
until now — Phase 11 is the first phase this is real.

## 1. What triggers an alert

| Event | Where | Why this one |
|---|---|---|
| Kill switch engaged | `apps/api/app/api/routes/risk.py` | The single highest-value alert in the system — trading has stopped, and anyone watching the channel should know immediately, not just whoever is looking at the dashboard right now. |
| Kill switch disengaged | same | Confirms trading has resumed. |
| Login lockout | `apps/api/app/api/routes/auth.py` | Fires once, on the transition into lockout (5 failed attempts in 15 minutes) — a real brute-force attempt against the single admin account is worth knowing about. Does **not** fire on every failed attempt before that (would be noise) or after (the account is already locked, so nothing new to report). |
| Any service's `error`/`critical` system event | every `services/*/app/main.py` | RPC health-check failures, WS reconnect exhaustion, order reconciliation failures, training-loop crashes, etc. — the same `SystemEvent` rows the dashboard's System Events page already shows, now also pushed proactively. `info`-severity events (`service_started`, `service_stopped`) do **not** alert — every service restart would otherwise spam the channel. |

Nothing else alerts. In particular, `RiskEvent` rows (every WAIT/NO_TRADE
decision writes one) are **not** alerted on — see
`services/decision-engine/app/evaluate.py` — because in normal operation
this happens on every single evaluation cycle; alerting there would drown
out everything else within minutes; the dashboard's Risk page is the right
place to review that history.

## 2. Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. You get back a token that looks like
   `123456789:AAExampleTokenTextHere`.
2. Get your chat id: message your new bot anything (it won't reply — that's
   expected), then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read
   `result[0].message.chat.id` from the JSON. For a group chat, add the bot
   to the group first and use the group's (negative) id the same way.
3. Set both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenTextHere
   TELEGRAM_CHAT_ID=987654321
   ```
4. Verify the bot itself works, independent of this codebase, before
   trusting it end-to-end:
   ```
   curl "https://api.telegram.org/bot<TOKEN>/getMe"
   curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
     -d "chat_id=<CHAT_ID>" -d "text=YonixAlpha test message"
   ```
   If the second command doesn't produce a message in the chat, the
   token/chat_id pair is wrong — fix that before assuming anything in this
   codebase is broken.

Leaving either variable blank is a fully supported, silent no-op —
`send_telegram_alert()` (`packages/core-py/yonixalpha_core/notify.py`)
checks both and returns `False` immediately without making a network call
if either is unset. Every one of this doc's trigger points calls that same
function, so there is exactly one place this behavior is implemented and
exactly one place it would need to change.

## 3. Failure behavior

A Telegram outage, a revoked bot token, or a network error must never
break the thing that triggered the alert — especially not the kill switch,
the one safety-critical write action in this system. `send_telegram_alert`
catches every exception internally and returns `False` rather than
raising; the kill switch routes additionally wrap the call in their own
`try`/`except` (`apps/api/app/api/routes/risk.py::_alert`) as a second line
of defense specifically because that endpoint's reliability matters more
than any other caller's. Every outcome (missing credentials, a non-200
response, a network error) is logged at `warning` level
(`telegram.alert.*` in the structured logs) so a persistently broken
integration is still visible somewhere, even though it never blocks
anything.

## 4. What's genuinely verified vs. not

**Verified for real, in this session:**
- The exact Bot API request shape (`POST
  https://api.telegram.org/bot<token>/sendMessage` with a `{chat_id, text}`
  JSON body) — tested against a real `httpx.MockTransport` asserting the
  URL and payload byte-for-byte
  (`packages/core-py/tests/test_notify.py`).
- Every failure path returns `False` rather than raising: unset
  credentials, a non-200 response, and a simulated network error are each
  their own test.
- Every trigger point (kill switch engage/disengage, login lockout) is
  covered by a test that monkeypatches `send_telegram_alert` and asserts
  it's called with the expected text, plus one test proving the kill
  switch endpoint still returns 200 even if the alert call itself raises
  (`apps/api/tests/test_risk.py`, `apps/api/tests/test_auth.py`).

**Not verified, and cannot be from this sandbox:**
- An actual message arriving in a real Telegram chat. This sandbox has no
  real bot token to test against — and beyond that, confirmed by directly
  testing it: `curl https://api.telegram.org/...` from here fails outright
  with `CONNECT tunnel failed, response 403` from this sandbox's own
  egress proxy ("organization policy"), before any request reaches
  Telegram at all. So this isn't just "untested" — this specific sandbox
  cannot reach `api.telegram.org` under any credentials, real or fake.
  That's a property of this sandbox's network policy, not of the
  integration code itself (which only depends on outbound HTTPS being
  reachable — the same as any other Telegram bot integration anywhere).
  Before relying on this in production, on a host that isn't
  network-restricted this way, run the `curl` commands in section 2
  against a real bot, then trigger one real alert end-to-end (e.g. engage
  the kill switch from a running `apps/api` instance with real credentials
  set) and confirm the message actually arrives.
