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
