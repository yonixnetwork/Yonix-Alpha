export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface CandidateSummary {
  id: string;
  token_id: string;
  mint_address: string;
  engine: string;
  state: string;
  state_updated_at: string;
  created_at: string;
}

export interface StateHistoryEntry {
  state: string;
  at: string;
  reason: string | null;
}

export interface SignalSummary {
  id: string;
  decision: string;
  confidence: string;
  entry_type: string | null;
  entry: string | null;
  stop_loss: string | null;
  take_profit: string[];
  risk_score: string;
  reason: string[];
  data_quality: string;
  created_at: string;
}

export interface RiskEventSummary {
  id: string;
  approved: boolean;
  reasons: string[];
  created_at: string;
}

export interface PaperPositionSummary {
  id: string;
  status: string;
  side: string;
  entry_price: string;
  exit_price: string | null;
  realized_pnl: string | null;
  realized_pnl_pct: string | null;
  exit_reason: string | null;
  entry_at: string;
  exit_at: string | null;
}

export interface CandidateDetail extends CandidateSummary {
  detail: Record<string, unknown> | null;
  state_history: StateHistoryEntry[];
  latest_signal: SignalSummary | null;
  latest_risk_event: RiskEventSummary | null;
  paper_position: PaperPositionSummary | null;
}

export interface StrategySignalOut {
  id: string;
  candidate_id: string | null;
  symbol: string;
  decision: string;
  confidence: string;
  entry_type: string | null;
  entry: string | null;
  stop_loss: string | null;
  take_profit: string[];
  risk_score: string;
  reason: string[];
  data_quality: string;
  created_at: string;
}

export interface RiskEventOut {
  id: string;
  candidate_id: string | null;
  symbol: string | null;
  approved: boolean;
  reasons: string[];
  context: Record<string, unknown> | null;
  created_at: string;
}

export interface KillSwitchStatus {
  engaged: boolean;
  reason: string | null;
}

export interface ModelVersionOut {
  id: string;
  name: string;
  version: number;
  status: string;
  feature_names: string[];
  training_sample_count: number;
  metrics: Record<string, unknown>;
  artifact_format: string;
  trained_at: string;
  activated_at: string | null;
  created_at: string;
}

export interface MLStatsOut {
  total_features: number;
  labeled_features: number;
  unlabeled_features: number;
}

export interface PaperPositionOut {
  id: string;
  candidate_id: string | null;
  symbol: string;
  provider: string;
  side: string;
  entry_price: string;
  quantity: string;
  stop_loss: string | null;
  take_profit: string[];
  status: string;
  exit_price: string | null;
  exit_reason: string | null;
  realized_pnl: string | null;
  realized_pnl_pct: string | null;
  entry_at: string;
  exit_at: string | null;
  created_at: string;
  engine?: string | null;
  asset_id?: string | null;
  assessment_id?: string | null;
  initial_quantity?: string | null;
  remaining_quantity?: string | null;
  entry_cost_quote?: string | null;
  proceeds_quote?: string | null;
  fees_paid_quote?: string | null;
  max_loss_quote?: string | null;
  tp_hits?: number[] | null;
  trailing_stop?: string | null;
  highest_price?: string | null;
  lowest_price?: string | null;
  last_price?: string | null;
}

export interface ServiceStatus {
  status: "running" | "stopped" | "unknown";
  last_event_at: string | null;
}

export interface SystemStatusOut {
  app_env: string;
  app_name: string;
  trading_enabled: boolean;
  live_trading_enabled: boolean;
  kill_switch: KillSwitchStatus;
  services: Record<string, ServiceStatus>;
}

export interface SystemEventOut {
  id: string;
  service: string;
  event_type: string;
  severity: string;
  detail: Record<string, unknown> | null;
  created_at: string;
}

// --- control center ---------------------------------------------------------

export interface SettingsOut {
  scope: string;
  effective: Record<string, string | number | boolean | string[] | null>;
  source: { scope: string; version: number; clamp_notes?: string[]; errors?: string[] };
  defaults: Record<string, string | number | boolean | string[] | null>;
  hard_limits: Record<string, { kind: "min" | "max"; bound: string }>;
}

export interface SettingsVersionOut {
  id: string;
  scope: string;
  version: number;
  settings: Record<string, unknown>;
  note: string | null;
  created_at: string;
}

export interface ModesOut {
  global_mode: "PAPER" | "MANUAL" | "LIVE";
  strategies: Record<string, "OFF" | "PAPER" | "MANUAL" | "AUTO">;
  env: { trading_enabled: boolean; live_trading_enabled: boolean; paper_trading: boolean; live_permitted: boolean };
}

export interface BlacklistOut {
  id: string;
  scope: string;
  field: string;
  match_type: string;
  value: string;
  reason: string | null;
  enabled: boolean;
  created_at: string;
}

export interface CustomRuleOut {
  id: string;
  name: string;
  scope: string;
  field: string;
  op: string;
  threshold: string;
  action: string;
  enabled: boolean;
  created_at: string;
}

export interface AssessmentSummary {
  id: string;
  candidate_id: string | null;
  engine: string;
  strategy: string;
  asset_id: string;
  symbol: string | null;
  decision: string;
  status_label: string;
  executable: boolean;
  execution_target: string;
  overall_risk: string;
  approval_state: string;
  reasons: string[];
  position_size: string | null;
  evaluated_at: string;
  outcome: Record<string, unknown> | null;
}

export interface Finding {
  category: string;
  code: string;
  level: string;
  message: string;
  action: string;
  hard_block: boolean;
}

export interface PlannedValue {
  value: string;
  provenance: "MANUAL" | "AUTO";
  method: string;
  inputs: Record<string, unknown>;
}

export interface TradePlanOut {
  entry_price: string | null;
  stop_loss: PlannedValue | null;
  stop_distance_pct: string | null;
  max_loss: PlannedValue | null;
  position_size: PlannedValue | null;
  quantity: string | null;
  take_profits: { price: PlannedValue; exit_fraction: string }[];
  trailing: { enabled: boolean; provenance: string; method: string; distance_pct: string | null; activation_price: string | null } | null;
  entry_cost_bps: string | null;
  exit_cost_bps: string | null;
  binding_cap: string | null;
}

export interface TimelineEvent {
  id: string;
  event_type: string;
  detail: Record<string, unknown> | null;
  occurred_at: string;
  position_id: string | null;
  assessment_id: string | null;
}

export interface AssessmentDetail extends AssessmentSummary {
  assessment: {
    findings: Finding[];
    plan: TradePlanOut;
    data_status: Record<string, string>;
    category_risk: Record<string, string>;
    versions: Record<string, unknown>;
    settings_snapshot: Record<string, unknown>;
    inputs_snapshot: Record<string, unknown>;
    qualified: boolean;
  };
  approved_at: string | null;
  timeline: TimelineEvent[];
}

export interface PaperAccountOut {
  name: string;
  quote_currency: string;
  starting_balance: string;
  cash_balance: string;
  equity: string;
  open_positions: number;
  open_exposure: string;
  closed_positions: number;
  winning_positions: number;
  realized_pnl: string;
  fees_paid: string;
  reset_at: string;
}

export interface PipelineOut {
  stream: { heartbeat: string | null; heartbeat_age_seconds: number | null; counters: Record<string, number>; tracked_recent_mints: number };
  funnel: Record<string, string>;
  candidates: Record<string, number>;
  decisions_24h: Record<string, number>;
  last_assessment_at: string | null;
}
