import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
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


# ---------------------------------------------------------------------------
# Phase 4: Binance USDT-M Futures execution/account state. Distinct from the
# Solana engines' Token/TokenEvent/TradingCandidate tables — these track a
# real exchange account, not discovered opportunities. No strategy/signal
# columns here: this is the mechanical execution/account layer per spec
# section 8 ("the strategy layer must be independent from the execution
# layer"); the shared risk/decision system that decides WHETHER to trade is
# Phase 5's table, not this one's.
# ---------------------------------------------------------------------------


class Order(Base):
    """One row per order intent, created BEFORE the exchange is ever called
    (spec section 21: "create client order ID, store intent, execute,
    store exchange transaction ID, reconcile, update status") — this is
    what makes crash-safe idempotent submission possible: a restart can
    find every `pending_submit` row and ask the exchange "what actually
    happened to this clientOrderId" rather than guessing or double-submitting.
    """

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    client_order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY|SELL
    position_side: Mapped[str] = mapped_column(String(8), nullable=False, default="BOTH")
    order_type: Mapped[str] = mapped_column(String(24), nullable=False)  # MARKET|LIMIT|STOP_MARKET|TAKE_PROFIT_MARKET|...
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Our own lifecycle starts at pending_submit/submit_failed (before/around
    # the exchange call); once confirmed it mirrors Binance's own status
    # values verbatim (NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED,
    # EXPIRED) rather than inventing a parallel vocabulary for them.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_submit", index=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    fills: Mapped[list["Fill"]] = relationship(back_populates="order")


class Position(Base):
    """One row per symbol (one-way position mode, Binance's default — no
    hedge-mode dual long/short rows). Always overwritten from the
    authoritative exchange state (GET /fapi/v2/positionRisk or an
    ACCOUNT_UPDATE stream event), never computed locally from fills — the
    exchange's own numbers (entry price, liquidation price, unrealized
    PnL) already account for funding, fees and any manual intervention a
    locally-derived calculation would miss.
    """

    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    position_amt: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False, default=0)  # signed: +long/-short/0=flat
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # isolated|cross
    liquidation_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Fill(Base):
    """One row per exchange trade (a single order can produce several,
    on partial fills). `order_id` is nullable because a fill can arrive on
    the user-data stream referencing a clientOrderId this process doesn't
    recognize (e.g. an order placed manually on the exchange, or from
    before this table existed) — still worth recording, just unlinked.
    """

    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    exchange_trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    commission_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    order: Mapped["Order | None"] = relationship(back_populates="fills")


class PnlRecord(Base):
    """One row per Binance "income" entry (GET /fapi/v1/income):
    REALIZED_PNL, FUNDING_FEE, COMMISSION, and other income/expense types
    Binance itself categorizes — never computed or inferred locally.
    `tran_id` is Binance's own transaction id for the income record, used
    for dedup on repeated polling.
    """

    __tablename__ = "pnl_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    income_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    income: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    tran_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Phase 5: shared decision system audit trail (spec section 19/51). Both
# tables are asset-class-agnostic — candidate_id is nullable because a
# future Binance-side signal has no TradingCandidate to reference (that
# table's FK is Solana-only, from Phase 3); `symbol` is always populated
# so either side can be queried without an optional join.
# ---------------------------------------------------------------------------


class StrategySignal(Base):
    """One row per Signal/Decision Engine evaluation — the structured
    Decision output from spec section 51, persisted verbatim as the full
    audit trail of "why did/didn't we trade." Never overwritten: a
    candidate re-evaluated later gets a new row, so the history of
    changing confidence/reason over time is preserved.
    """

    __tablename__ = "strategy_signals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # LONG|SHORT|WAIT|NO_TRADE
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    entry_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    entry: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    take_profit: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    risk_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    reason: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class RiskEvent(Base):
    """One row per Risk Engine evaluation, approved or rejected (spec
    sections 15/37/42: risk decisions are audited events). Stands apart
    from StrategySignal rather than being a column on it, since a risk
    check can be triggered by more than a signal-engine proposal (e.g. a
    future manual dashboard override).
    """

    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Phase 6: ML model registry + feature store. See docs/ML.md for why
# model_versions starts (and, honestly, is very likely to stay for a long
# time) empty of any `active` row: training requires labeled outcomes, and
# nothing in this codebase has ever closed a real or paper position, so
# ml_features.label is never anything but NULL yet — the column and the
# training pipeline that reads it are real, waiting on that data rather
# than inventing it (spec section 53).
# ---------------------------------------------------------------------------


class ModelVersion(Base):
    """One row per trained model artifact, append-only — training never
    overwrites a prior version, so a demoted model's metrics/artifact stay
    inspectable. `status` is the only mutable field: exactly one row per
    `name` may be `active` at a time (enforced by registry.activate_model,
    which retires any previous holder before promoting a new one — not by
    a DB constraint, since a brief multi-active window during that swap is
    harmless and a constraint would need deferred uniqueness). `artifact`
    is a joblib-serialized estimator stored directly in Postgres rather
    than a new object-storage dependency (S3/MinIO) this phase doesn't
    otherwise need.
    """

    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_model_versions_name_version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="trained")  # trained|active|retired
    feature_names: Mapped[list] = mapped_column(JSONB, nullable=False)
    training_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    artifact: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    artifact_format: Mapped[str] = mapped_column(String(16), nullable=False, default="joblib")
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MLFeatureSnapshot(Base):
    """One row per feature vector computed for a candidate at evaluation
    time — a genuine feature store: persisted independently of
    StrategySignal so a future training job can read a stable, versioned
    input space without recomputing from raw token_events. `label` is the
    supervised target (1 = profitable exit, 0 = not) and is always NULL
    today, set later by whatever future phase first closes a real
    position with a known outcome (see docs/ML.md) — never fabricated
    here to make training "work."
    """

    __tablename__ = "ml_features"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ml_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    label: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    label_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Phase 7: paper trading. See docs/PAPER_TRADING.md for why this table is
# expected to stay empty for the same reason model_versions.active stays
# empty (above): a paper position can only be opened at a real entry
# price, and no Decision this codebase produces has ever had one (Solana
# has no price feed to fill at — see decision-engine's own Phase 5 notes).
# The mechanism is real and tested; there is nothing to fabricate a price
# to make it "work" against.
# ---------------------------------------------------------------------------


class PaperPosition(Base):
    """One simulated position per opened candidate, whole lifecycle in a
    single row — unlike Binance's real Order/Fill split (Phase 4), there's
    no live exchange whose state could diverge from what this process
    itself decided, so there's nothing to reconcile after a crash: this
    row IS the source of truth. Closing it is also what backfills
    ml_features.label (see services/paper-trading/app/manage.py) — the
    first and only thing in this codebase that ever does.
    """

    __tablename__ = "paper_positions"
    # A position with a zero (or negative) entry price or quantity has an
    # undefined cost basis, which makes both position sizing at entry and
    # realized_pnl_pct at close undefined. Enforced here as well as in
    # services/paper-trading/app/entry.py so no future writer can
    # reintroduce the row shape that broke closing (and, through it, every
    # other open position's stop-loss) before this constraint existed.
    __table_args__ = (
        CheckConstraint("entry_price > 0", name="ck_paper_positions_entry_price_positive"),
        CheckConstraint("quantity > 0", name="ck_paper_positions_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # ExecutionProvider value at entry time
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG|SHORT
    entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    take_profit: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)  # open|closed
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    realized_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
