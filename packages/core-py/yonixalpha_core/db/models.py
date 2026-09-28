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
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="discovered")
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
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="trained")  # trained|challenger|active|superseded|retired|shadow (review only, never loaded for decisions)
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
    # Migration 0010: gate-era samples. `assessment_id` links a sample to the
    # decision that produced it (futures positions have no candidate);
    # `outcome` holds the explicit labels (TP hits, stop, MFE/MAE, return);
    # `quality_status` is set by the pre-training data-quality check.
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    feature_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    quality_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
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
        # A LIVE position exists before its fill (pending_entry, quantity 0);
        # it keeps 0 if the buy fails or confirms without tokens.
        CheckConstraint("quantity > 0 OR status IN ('pending_entry', 'failed', 'needs_review')",
                        name="ck_paper_positions_quantity_positive"),
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

    # Gate-driven positions (migration 0009). All nullable so rows opened by
    # the original Phase 7 flow stay valid. Amounts are in the paper
    # account's quote currency.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    asset_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    initial_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    remaining_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    entry_cost_quote: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    proceeds_quote: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    fees_paid_quote: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    max_loss_quote: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tp_hits: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    trailing_stop: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    highest_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    lowest_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    last_marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Operator controls (migration 0010). A paused position still honours
    # its stop; exit_requested is consumed by the next management tick.
    management_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    exit_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Migration 0011: execution provenance. PAPER positions fill in the
    # simulator; LIVE positions fill only from confirmed on-chain transactions
    # (status pending_entry until the buy confirms, needs_review when
    # the wallet disagrees with the record and a human must look).
    execution_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="PAPER", server_default="PAPER", index=True)
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # PUMPFUN | BINANCE | ...
    lifecycle: Mapped[str | None] = mapped_column(String(16), nullable=True)  # FRESH | MIGRATED
    execution_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)  # paper_simulator | pumpportal_local
    execution_route: Mapped[str | None] = mapped_column(String(32), nullable=True)  # pump | pump-amm | binance_book ...
    pool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pending_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    exit_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ExecutionOrder(Base):
    """One real (or simulated) execution request and its outcome. A LIVE
    order's signature is stored before the transaction is sent, so a crash
    at any point can be reconciled by looking the signature up on chain.
    `idempotency_key` makes a repeated decision unable to buy twice."""

    __tablename__ = "execution_orders"
    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL','RENT')", name="ck_execution_orders_side"),  # RENT: rent reclaim
        CheckConstraint("mode IN ('PAPER','LIVE')", name="ck_execution_orders_mode"),
        CheckConstraint("status IN ('PENDING','SIGNED','SUBMITTED','CONFIRMED','FAILED','EXPIRED','CANCELLED','SKIPPED')",
                        name="ck_execution_orders_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_positions.id", ondelete="SET NULL"), nullable=True, index=True)
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("risk_assessments.id", ondelete="SET NULL"), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)  # entry | stop_loss | take_profit_1 | ...
    mint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    route: Mapped[str] = mapped_column(String(16), nullable=False)  # pump | pump-amm
    amount: Mapped[str] = mapped_column(String(48), nullable=False)  # SOL for buys, raw token units for sells
    amount_kind: Mapped[str] = mapped_column(String(8), nullable=False)  # sol | tokens
    slippage_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    priority_fee_sol: Mapped[Decimal] = mapped_column(Numeric(20, 9), nullable=False)
    limits: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # guard bounds used
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    signature: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    guard: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Decision context at the BUY/SELL decision, stage timings and the
    # price-execution analysis (yonixalpha_core.execution_analysis).
    diagnostics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReconciliationEvent(Base):
    """What reconciliation found when it compared the wallet with the
    database: balance syncs, mismatches, unknown holdings, late
    confirmations, expired orders."""

    __tablename__ = "reconciliation_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    mint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_positions.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("execution_orders.id", ondelete="SET NULL"), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Control center: runtime risk configuration, operator rules, and the
# persisted output of the master safety gate (yonixalpha_core.safety).
# Secrets never live here — only tunable, non-secret configuration. The
# environment flags (TRADING_ENABLED / LIVE_TRADING_ENABLED / PAPER_TRADING)
# stay the outer bound: nothing stored in these tables can enable live
# trading on its own.
# ---------------------------------------------------------------------------


class RiskSettingsVersion(Base):
    """Append-only history of SafetySettings per scope ("GLOBAL" or an engine
    name such as "solana_fresh"). The effective settings for a scope are its
    highest version; editing inserts a new row, so every assessment's
    settings snapshot can be traced to the version that produced it."""

    __tablename__ = "risk_settings"
    __table_args__ = (UniqueConstraint("scope", "version", name="uq_risk_settings_scope_version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlatformSetting(Base):
    """Small key/value store for platform-wide runtime state, e.g.
    "global_mode" -> {"mode": "PAPER"}."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StrategyConfig(Base):
    """Per-strategy mode (OFF|PAPER|MANUAL|AUTO) and non-secret parameters."""

    __tablename__ = "strategy_configs"
    __table_args__ = (CheckConstraint("mode IN ('OFF','PAPER','MANUAL','AUTO')", name="ck_strategy_configs_mode"),)

    strategy: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="PAPER")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BlacklistEntry(Base):
    __tablename__ = "blacklist_rules"
    __table_args__ = (UniqueConstraint("scope", "field", "match_type", "value", name="uq_blacklist_rules_identity"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    field: Mapped[str] = mapped_column(String(16), nullable=False)  # name|symbol|mint|metadata|any
    match_type: Mapped[str] = mapped_column(String(16), nullable=False)  # exact|word|substring|pattern|regex
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="BLOCK", server_default="BLOCK")  # BLOCK|ALLOW
    value: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CustomRuleEntry(Base):
    __tablename__ = "custom_rules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(4), nullable=False)
    threshold: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RiskAssessment(Base):
    """One row per safety-gate evaluation, including every rejected and
    waiting opportunity, so rejections can be reviewed and their later
    outcome compared with what the gate decided. `idempotency_key` makes a
    retried evaluation of the same candidate at the same decision point a
    no-op rather than a duplicate."""

    __tablename__ = "risk_assessments"
    __table_args__ = (
        CheckConstraint(
            "approval_state IN ('NONE','PENDING','APPROVED','DECLINED','EXPIRED','IGNORED')",
            name="ck_risk_assessments_approval_state",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status_label: Mapped[str] = mapped_column(String(64), nullable=False)
    executable: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    execution_target: Mapped[str] = mapped_column(String(8), nullable=False)
    overall_risk: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_engine_version: Mapped[str] = mapped_column(String(16), nullable=False)
    assessment: Mapped[dict] = mapped_column(JSONB, nullable=False)  # Assessment.to_dict()
    approval_state: Mapped[str] = mapped_column(String(16), nullable=False, default="NONE", index=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Filled later for REJECT/NO_TRADE/WAIT rows: what the price did after
    # the decision (see safety follow-up job), never at decision time.
    outcome: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PaperAccount(Base):
    """Simulated balance per quote currency book (e.g. "solana" in SOL,
    "binance_futures" in USDT). Equity = cash + marked value of open
    positions; cash moves only on simulated fills and fees."""

    __tablename__ = "paper_accounts"
    __table_args__ = (CheckConstraint("starting_balance > 0", name="ck_paper_accounts_starting_balance_positive"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    quote_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    starting_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TradeTimelineEvent(Base):
    """Everything that happened to one opportunity, in order: assessed,
    waiting, approved, entered, TP hit, stop moved, exited. Read by the
    decision-detail page."""

    __tablename__ = "trade_timeline_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_candidates.id", ondelete="CASCADE"), nullable=True, index=True
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_positions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class PaperOrder(Base):
    """Resting and filled paper orders for strategies that work with limit
    orders (the grid). Market-order strategies fill instantly and record
    their fills on paper_positions/timeline instead."""

    __tablename__ = "paper_orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY|SELL
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)  # limit|market
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # open|filled|cancelled
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StrategyState(Base):
    """Durable per-strategy runtime state (spec: engine_states), e.g. a grid's
    levels, position and breaker status, so a restart resumes rather than
    rebuilding blindly."""

    __tablename__ = "strategy_states"
    __table_args__ = (UniqueConstraint("strategy", "key", name="uq_strategy_states_strategy_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Notification(Base):
    """In-app notification feed (spec §93). Telegram delivery is separate and
    optional; every notification is stored here regardless."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class DataQualityEvent(Base):
    """Records quarantined from ML training and why (spec §41)."""

    __tablename__ = "data_quality_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class TokenObservation(Base):
    """Outcome of the fresh pump.fun observation window for one token
    (solana.observation): promoted to the safety gate, rejected, expired
    (NO_TRADE) or handed to the migration engine. One row per token, written
    when the outcome is final, with the full context that decided it, so
    "why didn't the bot trade this token?" has an answer for every token the
    stream saw — not only the ones that reached the gate. Pruned after
    OBSERVATION_RETENTION_DAYS by the discovery service."""

    __tablename__ = "token_observations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    mint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    creator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    trend: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    report: Mapped[dict] = mapped_column(JSONB, nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Later snapshots of the same token (T+5m / T+10m / T+30m / T+60m after
    # launch, the migration event), filled by paper-trading's follow-up job:
    # {"T+30m": {"at", "price_raw", "change_pct", "liquidity_sol", "source"}, ...}.
    # Observation data only; never used for training by this job.
    followups: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class LiveSmokeTest(Base):
    """One armed LIVE_EXECUTION_SMOKE_TEST run (yonixalpha_core.live_smoke).
    The run only records the arming, the candidate attempts and the position
    it opened; buy/sell/confirmation status is always derived from the real
    execution_orders and paper_positions rows, never stored here."""

    __tablename__ = "live_smoke_tests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    category: Mapped[str] = mapped_column(String(16), nullable=False)  # FRESH / MIGRATED / MOMENTUM
    max_sol: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # ARMED / USED / EXPIRED / CANCELLED
    stage: Mapped[str | None] = mapped_column(String(48), nullable=True)
    stage_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    armed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_positions.id", ondelete="SET NULL"), nullable=True)
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OpportunityOutcome(Base):
    """Every opportunity the system decided on, traded or not, with the state
    it was decided on and what the market did afterwards (yonixalpha_core.
    opportunities). Observation data for learning and review only: nothing
    here is read by the gate, and no live rule changes from it."""

    __tablename__ = "opportunity_outcomes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    mint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # OBSERVATION | GATE
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    traded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    execution_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    horizons: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    peak_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    migrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trade_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    loss_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="TRACKING", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Ledger v2 (0019): the path to T+60m, theoretical vs executable return,
    # counterfactual / exit analysis, multi-target labels, regime tags and
    # shadow model scores (never read by the gate).
    path: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    labels: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    regime: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    post_exit: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ml_shadow: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    theoretical_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    executable_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LaunchBuyer(Base):
    """One early buyer of a launch the system decided on (yonixalpha_core.
    wallet_intel): what it bought, whether it sold in its first minutes, and
    the launch outcome once resolved. Reputation reads only rows whose
    outcome_resolved_at is before the decision it informs."""

    __tablename__ = "launch_buyers"
    __table_args__ = (
        UniqueConstraint("mint", "wallet", name="uq_launch_buyers_mint_wallet"),
        CheckConstraint("outcome IS NULL OR outcome IN ('WIN','FLAT','LOSS')", name="ck_launch_buyers_outcome"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    mint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wallet: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    launch_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_buy_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sol_in: Mapped[Decimal] = mapped_column(Numeric(20, 9), nullable=False)
    tokens_in: Mapped[Decimal] = mapped_column(Numeric(30, 0), nullable=False)
    sold_share_early: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    sold_early: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    early_window_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)  # WIN | FLAT | LOSS
    outcome_peak_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    outcome_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    outcome_migrated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outcome_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RpcProvider(Base):
    """A Solana RPC / WebSocket provider added from the dashboard
    (yonixalpha_core.solana.rpc_registry). URLs usually embed the API key,
    so they are stored encrypted (yonixalpha_core.secretbox) and only ever
    returned as scheme://host. Services reload this table on every
    configuration revision; no restart is needed."""

    __tablename__ = "rpc_providers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    chain: Mapped[str] = mapped_column(String(16), nullable=False, default="solana")
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")  # helius / alchemy / ...
    rpc_url_enc: Mapped[str] = mapped_column(String(2048), nullable=False)
    ws_url_enc: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rpc_display: Mapped[str] = mapped_column(String(256), nullable=False)
    ws_display: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=150)
    timeout_seconds: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("10"))
    rate_limit_rps: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_test: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
