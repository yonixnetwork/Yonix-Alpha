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

from sqlalchemy import (BigInteger, Boolean, DateTime, Integer, MetaData, Numeric, String, Text, UniqueConstraint,
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


def make_session_factory(database_url: str, *, pool_size: int = 2, max_overflow: int = 1):
    """A small pool: this engine needs a handful of queries a minute."""
    engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def schema_ready(session: AsyncSession) -> bool:
    """True once the crypto migrations have run (the API answers
    'not installed yet' instead of failing before that)."""
    r = await session.execute(text("SELECT to_regclass(:t)"), {"t": f"{SCHEMA}.strategy_configs"})
    return r.scalar_one() is not None
