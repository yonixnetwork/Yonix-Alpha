import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from yonixalpha_core.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    """The platform's operator account(s). Phase 1 seeds exactly one admin
    user from ADMIN_USERNAME/ADMIN_PASSWORD_HASH; the table supports more
    later without a migration.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Session(Base):
    """One row per issued refresh token, so a logout / session revocation is
    a real state change (checked on every refresh) rather than something we
    just hope the client forgets. Access tokens stay stateless (JWT expiry
    only); refresh tokens are the durable, revocable session.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_jti: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Every important action per docs/SECURITY.md section 42: login,
    config change, strategy enable/disable, live-trading toggle, order
    lifecycle events, emergency stop, model deployment, etc. Engines and
    the risk/decision layer write here starting Phase 3+; Phase 1 only
    wires the table and the auth events.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SystemEvent(Base):
    """Service-level health/error events (RPC failure, WS disconnect, API
    timeout, circuit-breaker trip, etc.) — distinct from AuditLog, which is
    user/operator actions. Populated starting with Phase 2's data
    infrastructure; the table exists from Phase 1 so nothing has to add a
    migration just to start logging service health.
    """

    __tablename__ = "system_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    service: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# ---------------------------------------------------------------------------
# Phase 2: normalized market-event storage. These are raw/near-raw ingestion
# tables (DISCOVERY + DATA NORMALIZATION stages of the pipeline in the spec)
# — no trading logic reads or writes these directly. Aggregated features
# (wallet concentration, volume z-score, etc.) are computed FROM this data by
# the feature engine starting Phase 5; they are not stored here as columns,
# since computing them requires decisions (window size, weighting) that
# belong to that layer, not to ingestion. token_metrics / wallets_activity /
# trades / orders / positions land with the phases that actually consume
# them (3-5), not speculatively here.
# ---------------------------------------------------------------------------


class Token(Base):
    """Canonical registry of every Solana mint address ever observed by
    ingestion, regardless of which engine (discovery/migration/momentum)
    first saw it. One row per mint; token_events holds the full history.
    """

    __tablename__ = "tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    mint_address: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    creator_address: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    first_seen_source: Mapped[str] = mapped_column(String(32), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["TokenEvent"]] = relationship(back_populates="token", cascade="all, delete-orphan")


class TokenEvent(Base):
    """Append-only log of every Solana on-chain event ingestion observes for
    a token: mint/creation, trade (buy/sell), migration/graduation,
    transfer. `payload` keeps the full raw source message (never discarded,
    per the spec's "no fabricated data" rule — anything not captured in a
    normalized column is still recoverable from here); the normalized
    columns are only the fields common enough across event types to be
    worth indexing on directly.
    """

    __tablename__ = "token_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # created|trade|migration|transfer
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # pumpportal|helius|rpc|...
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trader_address: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sol_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 9), nullable=True)
    token_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 0), nullable=True)
    is_buy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    token: Mapped["Token"] = relationship(back_populates="events")

    __table_args__ = (
        # token_id is part of the key, not just (source, signature, event_type):
        # a single transaction can create more than one mint (e.g. a
        # multi-mint CPI), producing multiple "created" TokenEvents that
        # legitimately share a signature but belong to different tokens.
        UniqueConstraint("source", "signature", "event_type", "token_id", name="uq_token_events_source_signature_type_token"),
    )


class MarketSnapshot(Base):
    """Generic normalized time-series point for any instrument from any
    source (Binance kline/trade/depth, Solana bonding-curve price point,
    ...). Deliberately source-agnostic — `symbol` is a Binance ticker
    ("BTCUSDT") or a Solana mint address, `source` disambiguates. Common
    numeric fields are normalized; everything else stays in `payload`.
    """

    __tablename__ = "market_snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # binance|solana
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # kline_1m|trade|depth|bonding_curve
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source", "symbol", "snapshot_type", "sequence", name="uq_market_snapshots_dedup"),
    )


class TradingCandidate(Base):
    """One row per opportunity an engine is tracking through the state
    machine in yonixalpha_core.state_machine (DISCOVERED -> ... -> CLOSED,
    or REJECTED at any point before ENTERED). `state_history` carries the
    full transition path with timestamps and reasons, so state is always
    reconstructable from Postgres after a restart rather than relying on
    RAM (spec section 10) — `state` alone is a cache of
    state_history[-1]["state"], never the source of truth on its own.

    No risk_score/confidence columns here: those are the Risk/Decision
    Engine's output (Phase 5), not something a discovery-stage engine
    produces. `detail` holds whatever engine-specific findings justified
    the DISCOVERED transition (e.g. the triggering signature, an
    acceleration ratio) for later inspection.
    """

    __tablename__ = "trading_candidates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # discovery|migration|momentum
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="discovered")
    state_history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    state_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    token: Mapped["Token"] = relationship()
