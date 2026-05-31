export type AgentBacktestProfileKey = 'short' | 'medium' | 'long' | string;

export interface AgentBacktestProfileItem {
  id: number;
  runId: number;
  accountId: number;
  profileKey: AgentBacktestProfileKey;
  displayName: string;
  styleProfile: string;
  policyVersionLabel: string;
  policyMarkdown?: string | null;
  latestPolicyId?: number | null;
  contextNamespace: string;
  status: string;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface AgentBacktestProfileCreateRequest {
  profileKey: string;
  displayName: string;
  styleProfile: string;
  policyVersionLabel?: string;
  policyMarkdown: string;
}

export type AgentBacktestMarket = 'cn' | 'us';

export interface AgentBacktestRunItem {
  id: number;
  name: string;
  status: string;
  market: AgentBacktestMarket | string;
  symbols: string[];
  symbolNames?: Record<string, string>;
  startDate?: string | null;
  endDate?: string | null;
  initialCashPerAgent: number;
  maxObservationsPerDay: number;
  ruleVersion: string;
  config: Record<string, unknown>;
  profiles: AgentBacktestProfileItem[];
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface AgentBacktestRunListResponse {
  items: AgentBacktestRunItem[];
  total: number;
}

export interface AgentBacktestRunUpdateRequest {
  symbols?: string[];
  maxObservationsPerDay?: number;
}

export interface AgentBacktestPolicyItem {
  id: number;
  runId: number;
  profileId: number;
  versionLabel: string;
  bodyMarkdown: string;
  parentPolicyId?: number | null;
  effectiveFrom?: string | null;
  changeReason?: string | null;
  status: string;
  createdAt?: string | null;
}

export interface AgentBacktestPolicyListResponse {
  items: AgentBacktestPolicyItem[];
  total: number;
}

export interface AgentBacktestObservationItem {
  id: number;
  runId: number;
  profileId: number;
  tradeDate?: string | null;
  observationTime: string;
  dataCutoffAt?: string | null;
  sequenceNo: number;
  symbols: string[];
  evidence: Record<string, unknown>;
  summary?: string | null;
  createdAt?: string | null;
}

export interface AgentBacktestDecisionItem {
  id: number;
  runId: number;
  profileId: number;
  observationId?: number | null;
  tradeDate?: string | null;
  decisionTime?: string | null;
  action: string;
  symbol?: string | null;
  side?: string | null;
  quantity?: number | null;
  orderType?: string | null;
  limitPrice?: number | null;
  confidence?: number | null;
  rationale?: string | null;
  riskNotes?: string | null;
  policyVersionLabel: string;
  rawOutput: Record<string, unknown>;
  createdAt?: string | null;
}

export interface AgentBacktestOrderItem {
  id: number;
  runId: number;
  profileId: number;
  decisionId?: number | null;
  symbol: string;
  side: string;
  orderType: string;
  requestedQuantity: number;
  limitPrice?: number | null;
  submittedAt?: string | null;
  effectiveAt?: string | null;
  status: string;
  rejectReason?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface AgentBacktestFillItem {
  id: number;
  runId: number;
  profileId: number;
  orderId: number;
  portfolioTradeId?: number | null;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  fee: number;
  tax: number;
  filledAt?: string | null;
  source: string;
  createdAt?: string | null;
}

export interface AgentBacktestDailyNavItem {
  id: number;
  runId: number;
  profileId: number;
  tradeDate?: string | null;
  cash: number;
  marketValue: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  valuationStale?: boolean;
  payload: Record<string, unknown>;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface AgentBacktestDailyNavResponse {
  runId: number;
  tradeDate: string;
  items: AgentBacktestDailyNavItem[];
}

export interface AgentBacktestEventsResponse {
  observations: AgentBacktestObservationItem[];
  decisions: AgentBacktestDecisionItem[];
  orders: AgentBacktestOrderItem[];
  fills: AgentBacktestFillItem[];
  dailyNav: AgentBacktestDailyNavItem[];
}
