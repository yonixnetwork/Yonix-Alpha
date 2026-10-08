/** Response shapes of the control-center endpoints (apps/api routes
 * analytics, strategies, venues, summary, notifications, system/health,
 * ml/review, paper, tokens). Decimals arrive as strings and stay strings
 * until formatted, so no precision is lost to floats. */

export type ConnState = "CONNECTED" | "DEGRADED" | "STALE" | "UNAVAILABLE" | "NOT CONFIGURED" | "DISABLED" | "UNKNOWN";

export interface Connection {
  name: string;
  category: string;
  state: ConnState;
  detail: string;
  [k: string]: unknown;
}

export interface HealthOut {
  overall: ConnState;
  connections: Connection[];
}

export interface SummaryAccount {
  name: string;
  currency: string;
  balance: string;
  available: string;
  equity: string | null;
  open_positions: number;
  exposure: string;
  realized_pnl_today: string;
  realized_pnl_since_reset: string;
}

export interface SummaryOut {
  accounts: SummaryAccount[];
  open_positions: number;
  global_mode: string;
  kill_switch: boolean;
  env: { trading_enabled: boolean; live_trading_enabled: boolean; paper_trading: boolean; live_permitted: boolean };
  system_status: ConnState;
  connections: Record<string, ConnState>;
  unread_notifications: number;
  at: string;
}

export interface Perf {
  trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number | null;
  loss_rate: number | null;
  total_pnl: string;
  gross_profit: string;
  gross_loss: string;
  profit_factor: string | null;
  expectancy: string | null;
  avg_win: string | null;
  avg_loss: string | null;
  largest_win: string | null;
  largest_loss: string | null;
  max_drawdown: string;
  max_drawdown_pct: string | null;
  fees: string;
  avg_duration_seconds: number | null;
  equity_curve?: { at: string; cumulative_pnl: string }[];
}

export interface AccountPerf {
  account: string;
  currency: string;
  starting_balance: string;
  reset_at: string;
  open_positions_not_counted: number;
  overall: Perf;
  by_strategy: Record<string, Perf>;
  by_engine: Record<string, Perf>;
  by_venue: Record<string, Perf>;
}

export interface PerformanceOut {
  accounts: AccountPerf[];
  notes: string[];
}

export interface StrategyOut {
  name: string;
  label: string;
  kind: "solana";
  status: string;
  engine: string | null;
  account: string | null;
  config: Record<string, unknown>;
  saved_config: Record<string, unknown>;
  editable: string[];
  mode?: string;
  effective_mode?: string;
  venue_mode_key?: string | null;
  currency?: string | null;
  open_positions?: number;
  last_decision_at?: string | null;
  trades?: number;
  win_rate?: number | null;
  total_pnl?: string;
  profit_factor?: string | null;
  max_drawdown?: string;
}

export interface ConfigModule {
  label: string;
  status: "DISABLED" | "READY" | "CONFIGURATION_ERROR";
  mode: string | null;
  errors: string[];
  live_missing: string[];
  live_ready: boolean | null;
  warnings: string[];
}

export interface ConfigValidationOut {
  modules: Record<string, ConfigModule>;
  note: string;
}

export interface NotificationOut {
  id: string;
  kind: string;
  severity: string;
  title: string;
  body: string | null;
  data: Record<string, unknown> | null;
  read_at: string | null;
  created_at: string;
}

export interface NotificationsPage {
  items: NotificationOut[];
  total: number;
  unread: number;
  limit: number;
  offset: number;
}

export interface ModelSummary {
  id: string;
  name: string;
  version: number;
  status: string;
  feature_names: string[];
  training_samples: number;
  trained_at: string;
  activated_at: string | null;
  metrics: Record<string, any>;
}

export interface ModelReview {
  model: string;
  engines: string[];
  feature_version: string;
  features: string[];
  champion: ModelSummary | null;
  challenger: ModelSummary | null;
  drift_flag: boolean;
  drift: Record<string, any> | null;
  samples: { total: number; labeled: number; quality_ok: number; quarantined: number; scored: number };
  mode: string;
}

export interface TradeDetail {
  position: Record<string, any>;
  account: { name: string; currency: string } | null;
  strategy: string | null;
  assessment: Record<string, any> | null;
  timeline: { type: string; at: string; detail: Record<string, unknown> | null }[];
  orders?: ExecutionOrderRow[];
}

export interface ExecutionOrderRow {
  id: string;
  side: "BUY" | "SELL";
  reason: string;
  status: string;
  route: string;
  provider: string;
  amount: string;
  amount_kind: string;
  slippage_pct: string;
  signature: string | null;
  error: string | null;
  fill: { sol_change_lamports: number; token_change_raw: number; fee_lamports: number } | null;
  created_at: string;
  confirmed_at: string | null;
  mint?: string;
  attempts?: number;
  position_id?: string | null;
  stage?: string | null;
  diagnostics?: ExecutionDiagnostics | null;
}

export interface ExecutionTiming {
  timestamps?: Record<string, string | null>;
  decision_eval_ms?: number | null; queue_wait_ms?: number | null; quote_latency_ms?: number | null; build_ms?: number | null;
  guard_and_recheck_ms?: number | null; simulation_ms?: number | null; submission_latency_ms?: number | null;
  submit_to_seen_ms?: number | null; submit_to_confirm_ms?: number | null; decision_to_submit_ms?: number | null;
  decision_to_confirm_ms?: number | null; rpc_latency_ms?: number | null; rpc_latency_before_submit_ms?: number | null;
  rpc_calls_before_submit?: number | null; slots_to_land?: number | null; discovery_to_decision_ms?: number | null;
  approval_to_order_ms?: number | null;
}

export interface ExecutionDiagnostics {
  decision?: Record<string, any> | null;
  timing?: ExecutionTiming;
  timing_reconstructed?: boolean;
  price?: {
    classification: string | null; evidence: string[]; components_pct: Record<string, string | null>;
    decision_price_sol?: string | null; spot_at_build_sol?: string | null; expected_price_sol?: string | null;
    spot_before_trade_sol?: string | null; trade_price_sol?: string | null; all_in_price_sol?: string | null;
    network_fee_sol?: string | null; priority_fee_sol?: string | null;
    costs_sol?: Record<string, string | null>;
  };
}
