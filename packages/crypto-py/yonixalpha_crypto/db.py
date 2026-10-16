"""Tables of the Crypto Trading engine, all in the Postgres schema `crypto`
with their own metadata (not yonixalpha_core's Base): no Memecoin table is
read or written, and the main Alembic chain never sees these tables
(its autogenerate compares only the default schema).

Every demo record is labelled by exchange and strategy and lives only here;
nothing in this schema touches a real balance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, MetaData, Numeric, String, Text, UniqueConstraint,
                        text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from yonixalpha_crypto import SCHEMA


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc)


AMOUNT = Numeric(38, 18)


class EngineState(Base):
    """One row (id 1): the Crypto Trading engine's own switch and emergency
    stop. Off by default; never turned on by a restart or a deploy."""
    __tablename__ = "engine_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class StrategyConfig(Base):
    """One row per strategy. A save changes only its own row (optimistic
    `version`), so one strategy's settings can never overwrite another's."""
    __tablename__ = "strategy_configs"
    strategy_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="DISABLED")  # DISABLED | DEMO
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbols: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    risk: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # trade-management plan (private.management.validate_plan): trailing, partials, time stop
    exits: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # EXCHANGE_DEMO mode: the practice (DEMO / TESTNET) connection its orders go to
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey(
        "crypto.api_connections.id", ondelete="SET NULL", name="fk_strategy_configs_connection_id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class ConfigAudit(Base):
    __tablename__ = "config_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    actor: Mapped[str | None] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(48), nullable=False)  # "engine" or a strategy id
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)


class Signal(Base):
    """One evaluation of one closed candle. The unique key makes a repeated
    evaluation of the same candle a no-op (duplicate-signal prevention)."""
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("strategy_id", "exchange", "symbol", "interval", "candle_open_time",
                                       name="uq_signal_candle"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG | SHORT | NONE
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # ENTER | EXIT | HOLD | REJECTED | NONE
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)


class DemoAccount(Base):
    __tablename__ = "demo_accounts"
    __table_args__ = (UniqueConstraint("exchange", "quote", name="uq_demo_account"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    quote: Mapped[str] = mapped_column(String(12), nullable=False)
    starting_balance: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    cash: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class DemoOrder(Base):
    """A simulated order. `client_id` is unique, so the same decision can
    never be filled twice (duplicate-order prevention)."""
    __tablename__ = "demo_orders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    strategy_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY | SELL
    status: Mapped[str] = mapped_column(String(12), nullable=False)  # FILLED | REJECTED
    reason: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal | None] = mapped_column(AMOUNT)
    price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    quote_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    fee_quote: Mapped[Decimal | None] = mapped_column(AMOUNT)
    slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    spread_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    book: Mapped[dict | None] = mapped_column(JSONB)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)


class DemoPosition(Base):
    __tablename__ = "demo_positions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False, default="LONG")
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="OPEN", index=True)  # OPEN | CLOSED
    qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    entry_cost_quote: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)  # incl. entry fee
    stop_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    target_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    fees_quote: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=Decimal(0))
    last_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    exit_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    proceeds_quote: Mapped[Decimal | None] = mapped_column(AMOUNT)  # net of exit fee
    realized_pnl_quote: Mapped[Decimal | None] = mapped_column(AMOUNT)
    exit_reason: Mapped[str | None] = mapped_column(String(48))
    # trade management in paper mode (strategy exits plan): the stop at entry,
    # the highest bid seen, which partial targets were taken, and the
    # realized P&L of partial exits while the position is still open
    initial_stop: Mapped[Decimal | None] = mapped_column(AMOUNT)
    highest_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    exits_state: Mapped[dict | None] = mapped_column(JSONB)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False)  # INFO | WARN | ERROR
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)


class ApiConnection(Base):
    """One exchange account key (private.connections). The key and secret
    are stored encrypted only; `key_fingerprint` (keyed HMAC) keeps one key
    from being saved twice, so a demo key and a live key can never be the
    same. Off by default; enabling it does not authorize any order."""
    __tablename__ = "api_connections"
    __table_args__ = (UniqueConstraint("exchange", "environment", "label", name="uq_api_connection_label"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(8), nullable=False)  # DEMO | TESTNET | LIVE
    label: Mapped[str] = mapped_column(String(48), nullable=False)
    key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_hint: Mapped[str] = mapped_column(String(12), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")  # UNTESTED | OK | FAILED | NEEDS_REENTRY
    permissions: Mapped[dict | None] = mapped_column(JSONB)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


EXEC_ACTIVE = ("PENDING_ENTRY", "OPEN", "CLOSING", "UNCERTAIN")  # exec_trades statuses that still need the worker


class ExecTrade(Base):
    """One managed trade on an exchange account (practice or, once unlocked,
    live). Written before any order is sent (private.executor), so a restart
    always finds what was attempted. `entry_key` (strategy + symbol + candle)
    is unique: the same signal can never open a second trade."""
    __tablename__ = "exec_trades"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crypto.api_connections.id",
                                                     ondelete="RESTRICT"), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(48), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    entry_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    # PENDING_ENTRY | OPEN | CLOSING | CLOSED | FAILED | UNCERTAIN
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    protection: Mapped[str] = mapped_column(String(16), nullable=False, default="NONE")  # NONE | PENDING | VERIFIED | MISSING
    protection_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qty_planned: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=0)  # held now (after fees and partial exits)
    entry_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    initial_stop: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    stop: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    stop_limit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(AMOUNT)
    highest_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    risk_budget: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    planned_loss: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # partials, trailing, time stop
    thesis: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    realized_quote: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=0)
    fees_quote: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=0)
    close_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ExecOrder(Base):
    """Every order the executor intends to send, recorded BEFORE sending
    (status PENDING_SEND), keyed by the client id the exchange also stores.
    A request with no answer becomes UNKNOWN and is resolved by looking the
    client id up, never by sending again."""
    __tablename__ = "exec_orders"
    client_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crypto.exec_trades.id", ondelete="RESTRICT"),
                                                nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(12), nullable=False)  # ENTRY | STOP | TP | EXIT
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    trigger_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    # PENDING_SEND | UNKNOWN | OPEN | PARTIAL | FILLED | CANCELED | REJECTED | EXPIRED | NOT_FOUND
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # part of the current protection
    exchange_id: Mapped[str | None] = mapped_column(String(64))
    filled_qty: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=0)
    avg_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=0)
    fee_asset: Mapped[str | None] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TradeAction(Base):
    """Every trade-management decision and execution step, with its reason."""
    __tablename__ = "trade_actions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crypto.exec_trades.id", ondelete="RESTRICT"),
                                                nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)


class ExecGuard(Base):
    """Per connection: new entries are blocked while the state is uncertain
    (unknown order, missing protection, balance mismatch). Also the row that
    serialises entries (SELECT ... FOR UPDATE)."""
    __tablename__ = "exec_guard"
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crypto.api_connections.id",
                                                     ondelete="CASCADE"), primary_key=True)
    entries_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class Ohlcv(Base):
    """Historical candles for backtests, with where they came from."""
    __tablename__ = "ohlcv"
    exchange: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    interval: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    high: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    low: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    close: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    volume: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class BacktestBatch(Base):
    __tablename__ = "backtest_batches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # QUEUED | RUNNING | DONE | CANCELLED
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestRun(Base):
    """One strategy on one symbol in one batch. `result` holds the full study
    (splits, walk-forward, sensitivity, benchmark, verdict); `summary` the
    few figures list views need. Every run records the code version, the
    configuration and the data it used."""
    __tablename__ = "backtest_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crypto.backtest_batches.id",
                                                ondelete="CASCADE"), nullable=False, index=True)
    strategy_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    # QUEUED | RUNNING | DONE | FAILED | CANCELLED | SKIPPED
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phase: Mapped[str | None] = mapped_column(String(48))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict | None] = mapped_column(JSONB)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortfolioSettings(Base):
    """One row (id 1): portfolio-wide limits over every strategy and mode,
    and the switches for optional heavy work (backtests, optimisation)."""
    __tablename__ = "portfolio_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    limits: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


def make_session_factory(database_url: str, *, pool_size: int = 2, max_overflow: int = 1):
    """A small pool: this engine needs a handful of queries a minute."""
    engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def schema_ready(session: AsyncSession) -> bool:
    """True once the crypto migrations have run (the API answers
    'not installed yet' instead of failing before that)."""
    r = await session.execute(text("SELECT to_regclass(:t)"), {"t": f"{SCHEMA}.strategy_configs"})
    return r.scalar_one() is not None
