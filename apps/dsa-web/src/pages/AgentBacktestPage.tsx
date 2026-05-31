import type React from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Moon, RefreshCw, Sun, X } from 'lucide-react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { agentBacktestApi } from '../api/agentBacktest';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import type {
  AgentBacktestDailyNavItem,
  AgentBacktestDecisionItem,
  AgentBacktestEventsResponse,
  AgentBacktestPolicyItem,
  AgentBacktestProfileItem,
  AgentBacktestRunItem,
} from '../types/agentBacktest';

type ProfileFilter = 'all' | string;
type TradingTheme = 'light' | 'dark';
type MarketFilter = 'cn' | 'us';
type TimelineType = 'observation' | 'decision' | 'order' | 'fill' | 'nav';
type CurveMode = 'return' | 'equity';

type TimelineItem = {
  id: string;
  at: string;
  type: TimelineType;
  profileId: number;
  title: string;
  detail: string;
  action?: string | null;
};

type CurvePoint = {
  date: string;
  short?: number;
  aggressive_short?: number;
  medium?: number;
  long?: number;
  [key: string]: number | string | undefined;
};

type ThemeStyle = React.CSSProperties & Record<string, string>;

type StockPoolItem = {
  symbol: string;
  name: string;
};

type PositionItem = {
  symbol?: string;
  quantity?: number;
  avgCost?: number;
  avg_cost?: number;
  totalCost?: number;
  total_cost?: number;
  lastPrice?: number;
  last_price?: number;
  marketValueBase?: number;
  market_value_base?: number;
  unrealizedPnlBase?: number;
  unrealized_pnl_base?: number;
  valuationDate?: string | null;
  valuation_date?: string | null;
  valuationStale?: boolean;
  valuation_stale?: boolean;
  [key: string]: unknown;
};

const PROFILE_ORDER = ['short', 'aggressive_short', 'medium', 'long'];
const MARKET_META: Record<MarketFilter, { label: string; shortLabel: string; currency: string; description: string }> = {
  cn: {
    label: 'A股',
    shortLabel: 'A',
    currency: '¥',
    description: 'A 股隔离操盘手的收益率与账户权益对比。每日 16:00 后用净值快照更新曲线。',
  },
  us: {
    label: '美股',
    shortLabel: 'US',
    currency: '$',
    description: '美股现金账户操盘手的收益率与账户权益对比，按 USD、T+1 settled cash 和盘外交易规则隔离。',
  },
};
const PROFILE_META: Record<string, { label: string; color: string; shortLabel: string }> = {
  short: { label: '短线操盘手', color: '#2563eb', shortLabel: '短线' },
  aggressive_short: { label: '激进短线操盘手', color: '#e11d48', shortLabel: '激进短线' },
  medium: { label: '中线操盘手', color: '#d97706', shortLabel: '中线' },
  long: { label: '长线操盘手', color: '#059669', shortLabel: '长线' },
};

const EMPTY_EVENTS: AgentBacktestEventsResponse = {
  observations: [],
  decisions: [],
  orders: [],
  fills: [],
  dailyNav: [],
};

function getStoredTheme(): TradingTheme {
  if (typeof window === 'undefined') return 'dark';
  return window.localStorage.getItem('trading-theme') === 'light' ? 'light' : 'dark';
}

function getThemeStyle(theme: TradingTheme): ThemeStyle {
  if (theme === 'dark') {
    return {
      '--trade-bg': '#111315',
      '--trade-panel': '#171a1d',
      '--trade-panel-soft': '#1d2125',
      '--trade-fg': '#f4f5f6',
      '--trade-muted': '#ffffff',
      '--trade-subtle': '#ffffff',
      '--trade-readable': '#ffffff',
      '--trade-border': '#2b3036',
      '--trade-hover': '#23282e',
      '--trade-grid': '#2a2f35',
      '--trade-positive': '#10b981',
      '--trade-negative': '#ef4444',
      '--foreground': '210 33% 98%',
    };
  }
  return {
    '--trade-bg': '#f7f8fa',
    '--trade-panel': '#ffffff',
    '--trade-panel-soft': '#f1f3f5',
    '--trade-fg': '#171a1d',
    '--trade-muted': '#58616a',
    '--trade-subtle': '#87909a',
    '--trade-readable': '#26313d',
    '--trade-border': '#dfe3e7',
    '--trade-hover': '#eef1f4',
    '--trade-grid': '#e7ebef',
    '--trade-positive': '#047857',
    '--trade-negative': '#dc2626',
    '--foreground': '228 35% 12%',
  };
}

function formatMoney(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatPrice(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 3,
  });
}

function formatQuantity(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toLocaleString('zh-CN', {
    maximumFractionDigits: 0,
  });
}

function formatCompactMoney(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  const numeric = Number(value);
  if (Math.abs(numeric) >= 10000) {
    return `${(numeric / 10000).toFixed(1)}万`;
  }
  return numeric.toLocaleString('zh-CN', {
    maximumFractionDigits: 0,
  });
}

function getCurrencySymbol(market?: string | null): string {
  return market === 'us' ? MARKET_META.us.currency : MARKET_META.cn.currency;
}

function formatSignedPct(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  const prefix = value > 0 ? '+' : '';
  return `${prefix}${value.toFixed(2)}%`;
}

function formatConfidence(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${Math.round(value * 100)}%`;
}

function formatMaybeDateTime(value?: string | null): string {
  if (!value) return '--';
  const normalized = value.includes('T') ? value : value.replace(' ', 'T');
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatTradeDate(value?: string | null): string {
  if (!value) return '--';
  return value.slice(5) || value;
}

function getProfileMeta(profileKey: string): { label: string; color: string; shortLabel: string } {
  return PROFILE_META[profileKey] || { label: profileKey, color: '#64748b', shortLabel: profileKey };
}

function getProfileLabel(profile?: AgentBacktestProfileItem): string {
  return profile?.displayName || profile?.profileKey || '--';
}

function getProfileJoinedAt(profile?: AgentBacktestProfileItem): string {
  return formatMaybeDateTime(profile?.createdAt);
}

function sortProfiles(profiles: AgentBacktestProfileItem[]): AgentBacktestProfileItem[] {
  return [...profiles].sort((a, b) => {
    const left = PROFILE_ORDER.indexOf(a.profileKey);
    const right = PROFILE_ORDER.indexOf(b.profileKey);
    return (left === -1 ? 99 : left) - (right === -1 ? 99 : right);
  });
}

function latestNavByProfile(items: AgentBacktestDailyNavItem[]): Map<number, AgentBacktestDailyNavItem> {
  const map = new Map<number, AgentBacktestDailyNavItem>();
  for (const item of items) {
    const current = map.get(item.profileId);
    if (!current || String(item.tradeDate || '') >= String(current.tradeDate || '')) {
      map.set(item.profileId, item);
    }
  }
  return map;
}

function positionCount(nav?: AgentBacktestDailyNavItem): number {
  const positions = (nav?.payload as { positions?: unknown } | undefined)?.positions;
  return Array.isArray(positions) ? positions.length : 0;
}

function getPositions(nav?: AgentBacktestDailyNavItem): PositionItem[] {
  const positions = (nav?.payload as { positions?: unknown } | undefined)?.positions;
  if (!Array.isArray(positions)) return [];
  return positions.filter((item): item is PositionItem => Boolean(item && typeof item === 'object'));
}

function getPositionNumber(position: PositionItem, ...keys: string[]): number | null {
  for (const key of keys) {
    const raw = position[key];
    if (raw == null || raw === '') continue;
    const value = Number(raw);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

function getReturnPct(nav: AgentBacktestDailyNavItem | undefined, initialCash: number): number | null {
  if (!nav || !initialCash) return null;
  return ((nav.totalEquity - initialCash) / initialCash) * 100;
}

function getPositionReturnPct(position: PositionItem): number | null {
  const avgCost = getPositionNumber(position, 'avgCost', 'avg_cost');
  const lastPrice = getPositionNumber(position, 'lastPrice', 'last_price');
  if (avgCost == null || lastPrice == null || avgCost <= 0) return null;
  return ((lastPrice - avgCost) / avgCost) * 100;
}

function latestDecisionByProfile(items: AgentBacktestDecisionItem[]): Map<number, AgentBacktestDecisionItem> {
  const map = new Map<number, AgentBacktestDecisionItem>();
  for (const item of items) {
    const current = map.get(item.profileId);
    const itemAt = item.decisionTime || item.createdAt || '';
    const currentAt = current?.decisionTime || current?.createdAt || '';
    if (!current || itemAt >= currentAt) {
      map.set(item.profileId, item);
    }
  }
  return map;
}

function sortDecisionsDesc(items: AgentBacktestDecisionItem[]): AgentBacktestDecisionItem[] {
  return [...items].sort((a, b) => {
    const left = a.decisionTime || a.createdAt || '';
    const right = b.decisionTime || b.createdAt || '';
    return String(right).localeCompare(String(left));
  });
}

function buildCurvePoints(
  navs: AgentBacktestDailyNavItem[],
  profiles: AgentBacktestProfileItem[],
  initialCash: number,
  mode: CurveMode,
): CurvePoint[] {
  const profileById = new Map(profiles.map((profile) => [profile.id, profile]));
  const byDate = new Map<string, CurvePoint>();

  for (const item of navs) {
    const profile = profileById.get(item.profileId);
    if (!profile || !item.tradeDate) continue;
    const point = byDate.get(item.tradeDate) || { date: item.tradeDate };
    if (mode === 'return') {
      if (!initialCash) continue;
      point[profile.profileKey] = Number((((item.totalEquity - initialCash) / initialCash) * 100).toFixed(4));
    } else {
      point[profile.profileKey] = Number(item.totalEquity.toFixed(2));
    }
    byDate.set(item.tradeDate, point);
  }

  return [...byDate.values()].sort((left, right) => left.date.localeCompare(right.date));
}

function buildTimeline(events: AgentBacktestEventsResponse, initialCash: number): TimelineItem[] {
  const observations = events.observations.map((item) => ({
    id: `observation-${item.id}`,
    at: item.createdAt || `${item.tradeDate || ''}T${item.observationTime}`,
    type: 'observation' as const,
    profileId: item.profileId,
    title: `观察 ${item.sequenceNo}`,
    detail: item.summary || `${item.symbols.length} 只标的，截点 ${formatMaybeDateTime(item.dataCutoffAt)}`,
  }));
  const decisions = events.decisions.map((item) => ({
    id: `decision-${item.id}`,
    at: item.decisionTime || item.createdAt || '',
    type: 'decision' as const,
    profileId: item.profileId,
    title: '决策',
    detail: [item.symbol, item.rationale].filter(Boolean).join(' · ') || item.policyVersionLabel,
    action: item.action,
  }));
  const orders = events.orders.map((item) => ({
    id: `order-${item.id}`,
    at: item.submittedAt || item.createdAt || '',
    type: 'order' as const,
    profileId: item.profileId,
    title: `订单 ${item.status}`,
    detail: `${item.side} ${item.symbol} ${item.requestedQuantity} @ ${item.limitPrice ?? 'market'}`,
  }));
  const fills = events.fills.map((item) => ({
    id: `fill-${item.id}`,
    at: item.filledAt || item.createdAt || '',
    type: 'fill' as const,
    profileId: item.profileId,
    title: '成交',
    detail: `${item.side} ${item.symbol} ${item.quantity} @ ${item.price}`,
  }));
  const navs = events.dailyNav.map((item) => ({
    id: `nav-${item.id}`,
    at: item.updatedAt || item.createdAt || item.tradeDate || '',
    type: 'nav' as const,
    profileId: item.profileId,
    title: '16:00 净值',
    detail: `权益 ${formatMoney(item.totalEquity)}，收益 ${formatSignedPct(getReturnPct(item, initialCash) ?? 0)}`,
  }));

  return [...observations, ...decisions, ...orders, ...fills, ...navs]
    .sort((left, right) => String(right.at).localeCompare(String(left.at)));
}

function typeLabel(type: TimelineType): string {
  if (type === 'observation') return '观察';
  if (type === 'decision') return '决策';
  if (type === 'order') return '订单';
  if (type === 'fill') return '成交';
  return '净值';
}

function actionLabel(action?: string | null): string {
  if (action === 'buy') return '买入';
  if (action === 'sell') return '卖出';
  if (action === 'hold') return '持有';
  if (action === 'observe') return '观察';
  return action || '--';
}

function actionTone(action?: string | null): { color: string; background: string; border: string } {
  if (action === 'buy') {
    return {
      color: 'var(--trade-positive)',
      background: 'rgba(16, 185, 129, 0.12)',
      border: 'rgba(16, 185, 129, 0.36)',
    };
  }
  if (action === 'sell') {
    return {
      color: 'var(--trade-negative)',
      background: 'rgba(239, 68, 68, 0.12)',
      border: 'rgba(239, 68, 68, 0.36)',
    };
  }
  if (action === 'hold') {
    return {
      color: '#f59e0b',
      background: 'rgba(245, 158, 11, 0.12)',
      border: 'rgba(245, 158, 11, 0.36)',
    };
  }
  return {
    color: 'var(--trade-muted)',
    background: 'transparent',
    border: 'var(--trade-border)',
  };
}

type ActionBadgeProps = {
  action?: string | null;
  symbol?: string | null;
};

const ActionBadge: React.FC<ActionBadgeProps> = ({ action, symbol }) => {
  const tone = actionTone(action);
  return (
    <span
      className="inline-flex max-w-full items-center gap-1 border px-1.5 py-0.5 text-xs font-semibold leading-5 tabular-nums"
      style={{ color: tone.color, backgroundColor: tone.background, borderColor: tone.border }}
    >
      <span>{actionLabel(action)}</span>
      {symbol ? <span className="truncate font-mono">{symbol}</span> : null}
    </span>
  );
};

function toggleStringSet(
  setter: React.Dispatch<React.SetStateAction<Set<string>>>,
  key: string,
): void {
  setter((current) => {
    const next = new Set(current);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    return next;
  });
}

function toggleNumberSet(
  setter: React.Dispatch<React.SetStateAction<Set<number>>>,
  key: number,
): void {
  setter((current) => {
    const next = new Set(current);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    return next;
  });
}

type ExpandableTextProps = {
  text?: string | null;
  expanded: boolean;
  onToggle: () => void;
  className?: string;
  collapsedLines?: number;
  ariaLabel: string;
};

const ExpandableText: React.FC<ExpandableTextProps> = ({
  text,
  expanded,
  onToggle,
  className = '',
  collapsedLines = 2,
  ariaLabel,
}) => {
  const contentRef = useRef<HTMLParagraphElement | null>(null);
  const [canExpand, setCanExpand] = useState(false);
  const [maxHeight, setMaxHeight] = useState<number | undefined>(undefined);
  const displayText = text?.trim() || '--';

  const measure = useCallback(() => {
    const element = contentRef.current;
    if (!element) return;
    const styles = window.getComputedStyle(element);
    const fontSize = Number.parseFloat(styles.fontSize || '14') || 14;
    const lineHeight = Number.parseFloat(styles.lineHeight || '') || fontSize * 1.55;
    const collapsedHeight = Math.ceil(lineHeight * collapsedLines);
    const fullHeight = element.scrollHeight;
    const hasOverflow = fullHeight > collapsedHeight + 2;

    setCanExpand(hasOverflow);
    setMaxHeight(hasOverflow ? (expanded ? fullHeight : collapsedHeight) : undefined);
  }, [collapsedLines, expanded]);

  useLayoutEffect(() => {
    measure();
  }, [displayText, measure]);

  useEffect(() => {
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [measure]);

  const toggle = () => {
    if (canExpand) {
      onToggle();
    }
  };

  return (
    <div
      role={canExpand ? 'button' : undefined}
      tabIndex={canExpand ? 0 : undefined}
      aria-label={canExpand ? ariaLabel : undefined}
      aria-expanded={canExpand ? expanded : undefined}
      title={canExpand ? '点击展开或收起' : undefined}
      onClick={(event) => {
        event.stopPropagation();
        toggle();
      }}
      onKeyDown={(event) => {
        if (!canExpand) return;
        event.stopPropagation();
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onToggle();
        }
      }}
      className={`overflow-hidden outline-none ${
        canExpand ? 'cursor-pointer rounded-[2px] focus-visible:ring-1 focus-visible:ring-[var(--trade-muted)]' : ''
      }`}
      style={{
        maxHeight,
        transition: 'max-height 260ms cubic-bezier(0.22, 1, 0.36, 1)',
      }}
    >
      <p
        ref={contentRef}
        className={`${className} ${canExpand ? 'transition-colors duration-200 hover:text-[var(--trade-fg)]' : ''}`}
      >
        {displayText}
      </p>
    </div>
  );
};

type HoldingsPanelProps = {
  positions: PositionItem[];
  symbolNames?: Record<string, string>;
};

const HoldingsPanel: React.FC<HoldingsPanelProps> = ({ positions, symbolNames }) => (
  <div>
    <div className="flex items-center justify-between gap-3">
      <h3 className="text-sm font-semibold text-[color:var(--trade-fg)]">持仓明细</h3>
      <span className="text-xs text-[color:var(--trade-subtle)]">{positions.length}</span>
    </div>
    {positions.length === 0 ? (
      <p className="mt-3 text-sm text-[color:var(--trade-readable)]">暂无持仓</p>
    ) : (
      <div className="mt-3 overflow-x-auto border border-[var(--trade-border)] bg-[var(--trade-panel-soft)]">
        <table className="w-full table-fixed border-collapse text-left text-[11px] sm:text-xs">
          <thead className="border-b border-[var(--trade-border)] text-[color:var(--trade-muted)]">
            <tr>
              <th scope="col" className="w-[24%] px-2 py-2 font-medium">股票</th>
              <th scope="col" className="w-[10%] px-2 py-2 text-right font-medium">数量</th>
              <th scope="col" className="w-[13%] px-2 py-2 text-right font-medium">建仓均价</th>
              <th scope="col" className="w-[12%] px-2 py-2 text-right font-medium">当前价</th>
              <th scope="col" className="w-[12%] px-2 py-2 text-right font-medium">涨跌幅</th>
              <th scope="col" className="w-[16%] px-2 py-2 text-right font-medium">成本金额</th>
              <th scope="col" className="w-[13%] px-2 py-2 text-right font-medium">浮盈</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--trade-border)]">
            {positions.map((position) => {
              const symbol = String(position.symbol || '');
              const symbolName = symbolNames?.[symbol] || '';
              const positionReturnPct = getPositionReturnPct(position);
              const quantity = getPositionNumber(position, 'quantity');
              const avgCost = getPositionNumber(position, 'avgCost', 'avg_cost');
              const totalCost = getPositionNumber(position, 'totalCost', 'total_cost');
              const lastPrice = getPositionNumber(position, 'lastPrice', 'last_price');
              const unrealizedPnl = getPositionNumber(position, 'unrealizedPnlBase', 'unrealized_pnl_base');
              const valuationStale = Boolean(position.valuationStale ?? position.valuation_stale);
              const valuationDate = position.valuationDate ?? position.valuation_date ?? '--';
              return (
                <tr key={symbol} className="align-top">
                  <td className="px-2 py-2">
                    <p className="font-mono text-xs text-[color:var(--trade-fg)] sm:text-sm">{symbol}</p>
                    <p className="mt-0.5 truncate text-[11px] text-[color:var(--trade-muted)] sm:text-xs">{symbolName || '名称待补'}</p>
                    {valuationStale ? (
                      <p className="mt-1 text-[11px] leading-4 text-[color:var(--trade-subtle)]">
                        估值日期 {valuationDate}
                      </p>
                    ) : null}
                  </td>
                  <td className="px-2 py-2 text-right font-medium tabular-nums text-[color:var(--trade-fg)]">{formatQuantity(quantity)}</td>
                  <td className="px-2 py-2 text-right font-medium tabular-nums text-[color:var(--trade-fg)]">{formatPrice(avgCost)}</td>
                  <td className="px-2 py-2 text-right font-medium tabular-nums text-[color:var(--trade-fg)]">{formatPrice(lastPrice)}</td>
                  <td
                    className="px-2 py-2 text-right font-semibold tabular-nums"
                    style={{ color: (positionReturnPct ?? 0) >= 0 ? 'var(--trade-positive)' : 'var(--trade-negative)' }}
                  >
                    {formatSignedPct(positionReturnPct)}
                  </td>
                  <td className="px-2 py-2 text-right font-medium tabular-nums text-[color:var(--trade-fg)]">{formatMoney(totalCost)}</td>
                  <td
                    className="px-2 py-2 text-right font-semibold tabular-nums"
                    style={{ color: (unrealizedPnl ?? 0) >= 0 ? 'var(--trade-positive)' : 'var(--trade-negative)' }}
                  >
                    {formatMoney(unrealizedPnl)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    )}
  </div>
);

type ProfileDetailDialogProps = {
  profile: AgentBacktestProfileItem;
  profileIndex: number;
  profileCount: number;
  nav?: AgentBacktestDailyNavItem;
  decision?: AgentBacktestDecisionItem;
  decisionHistory: AgentBacktestDecisionItem[];
  policyVersions: AgentBacktestPolicyItem[];
  run: AgentBacktestRunItem | null;
  onPreviousProfile: () => void;
  onNextProfile: () => void;
  onClose: () => void;
};

const ProfileDetailDialog: React.FC<ProfileDetailDialogProps> = ({
  profile,
  profileIndex,
  profileCount,
  nav,
  decision,
  decisionHistory,
  policyVersions,
  run,
  onPreviousProfile,
  onNextProfile,
  onClose,
}) => {
  const policyOptions = useMemo<AgentBacktestPolicyItem[]>(() => {
    if (policyVersions.length > 0) return policyVersions;
    return [
      {
        id: profile.latestPolicyId ?? 0,
        runId: profile.runId,
        profileId: profile.id,
        versionLabel: profile.policyVersionLabel,
        bodyMarkdown: profile.policyMarkdown || '暂无策略正文',
        parentPolicyId: null,
        effectiveFrom: null,
        changeReason: null,
        status: 'active',
        createdAt: profile.updatedAt || profile.createdAt || null,
      },
    ];
  }, [
    policyVersions,
    profile.createdAt,
    profile.id,
    profile.latestPolicyId,
    profile.policyMarkdown,
    profile.policyVersionLabel,
    profile.runId,
    profile.updatedAt,
  ]);
  const [selectedPolicyId, setSelectedPolicyId] = useState<number | null>(null);
  const [policyMenuOpen, setPolicyMenuOpen] = useState(false);
  const meta = getProfileMeta(profile.profileKey);
  const positions = getPositions(nav);
  const currentEquity = nav?.totalEquity ?? run?.initialCashPerAgent ?? null;
  const returnPct = nav ? getReturnPct(nav, run?.initialCashPerAgent || 0) : 0;
  const previousDecisions = decisionHistory.filter((item) => item.id !== decision?.id).slice(0, 8);
  const selectedPolicy = policyOptions.find((item) => item.id === selectedPolicyId) || policyOptions[0];
  const titleId = `profile-detail-title-${profile.id}`;
  const canSwitchProfile = profileCount > 1 && profileIndex >= 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-[2px]"
      onClick={onClose}
    >
      <div className="relative grid w-full max-w-[calc(100vw-2rem)] grid-cols-1 items-center md:grid-cols-[56px_minmax(0,64rem)_56px] md:justify-center md:gap-10">
        <button
          type="button"
          aria-label="上一位操盘手"
          title="上一位操盘手"
          disabled={!canSwitchProfile}
          onClick={(event) => {
            event.stopPropagation();
            onPreviousProfile();
          }}
          className="absolute left-2 top-1/2 z-10 grid h-16 w-14 -translate-y-1/2 place-items-center border border-[var(--trade-border)] bg-[var(--trade-panel)] text-[color:var(--trade-muted)] shadow-xl transition-colors hover:bg-[var(--trade-hover)] hover:text-[color:var(--trade-fg)] disabled:cursor-not-allowed disabled:opacity-35 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--trade-muted)] md:static md:col-start-1 md:row-start-1 md:translate-y-0"
        >
          <ChevronLeft size={28} strokeWidth={1.8} />
        </button>
        <button
          type="button"
          aria-label="下一位操盘手"
          title="下一位操盘手"
          disabled={!canSwitchProfile}
          onClick={(event) => {
            event.stopPropagation();
            onNextProfile();
          }}
          className="absolute right-2 top-1/2 z-10 grid h-16 w-14 -translate-y-1/2 place-items-center border border-[var(--trade-border)] bg-[var(--trade-panel)] text-[color:var(--trade-muted)] shadow-xl transition-colors hover:bg-[var(--trade-hover)] hover:text-[color:var(--trade-fg)] disabled:cursor-not-allowed disabled:opacity-35 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--trade-muted)] md:static md:col-start-3 md:row-start-1 md:translate-y-0"
        >
          <ChevronRight size={28} strokeWidth={1.8} />
        </button>
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-5xl border border-[var(--trade-border)] bg-[var(--trade-panel)] shadow-2xl outline-none md:col-start-2 md:row-start-1"
        style={{ maxHeight: 'calc(100vh - 32px)' }}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-[var(--trade-border)] px-5 py-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="h-2.5 w-2.5 shrink-0" style={{ backgroundColor: meta.color }} />
              <h2 id={titleId} className="truncate text-lg font-semibold text-[color:var(--trade-fg)]">
                {profile.displayName}
              </h2>
            </div>
            <p className="mt-1 text-xs leading-5 text-[color:var(--trade-subtle)]">
              {meta.shortLabel} · {profile.status} · 加入 {getProfileJoinedAt(profile)}
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭持仓明细"
            onClick={onClose}
            className="grid h-8 w-8 shrink-0 place-items-center border border-[var(--trade-border)] text-[color:var(--trade-muted)] transition-colors hover:bg-[var(--trade-hover)] hover:text-[color:var(--trade-fg)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--trade-muted)]"
          >
            <X size={17} strokeWidth={1.8} />
          </button>
        </div>
        <div className="overflow-y-auto px-5 py-5" style={{ maxHeight: 'calc(100vh - 132px)' }}>
          <dl className="grid grid-cols-2 gap-px bg-[var(--trade-border)] text-sm sm:grid-cols-4">
            <div className="bg-[var(--trade-panel)] p-3">
              <dt className="text-xs text-[color:var(--trade-muted)]">当前收益</dt>
              <dd
                className="mt-1 text-lg font-semibold tabular-nums"
                style={{ color: (returnPct ?? 0) >= 0 ? 'var(--trade-positive)' : 'var(--trade-negative)' }}
              >
                {formatSignedPct(returnPct)}
              </dd>
            </div>
            <div className="bg-[var(--trade-panel)] p-3">
              <dt className="text-xs text-[color:var(--trade-muted)]">当前权益</dt>
              <dd className="mt-1 text-base font-semibold tabular-nums" style={{ color: 'var(--trade-fg)' }}>
                {formatMoney(currentEquity)}
              </dd>
            </div>
            <div className="bg-[var(--trade-panel)] p-3">
              <dt className="text-xs text-[color:var(--trade-muted)]">策略版本</dt>
              <dd className="mt-1 font-medium text-[color:var(--trade-fg)]">{profile.policyVersionLabel}</dd>
            </div>
            <div className="bg-[var(--trade-panel)] p-3">
              <dt className="text-xs text-[color:var(--trade-muted)]">持仓数</dt>
              <dd className="mt-1 font-medium tabular-nums text-[color:var(--trade-fg)]">{positions.length}</dd>
            </div>
          </dl>

          <div className="mt-4">
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-[color:var(--trade-fg)]">当前策略</h3>
              <div className="relative">
                <button
                  type="button"
                  aria-expanded={policyMenuOpen}
                  aria-label={`切换${profile.displayName}策略版本`}
                  onClick={() => setPolicyMenuOpen((current) => !current)}
                  className="h-5 border border-[var(--trade-border)] bg-[var(--trade-panel-soft)] px-1.5 py-0 text-xs font-medium leading-4 tabular-nums text-[color:var(--trade-subtle)] transition-colors hover:bg-[var(--trade-hover)] hover:text-[color:var(--trade-fg)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--trade-muted)]"
                  style={{ fontSize: '12px', lineHeight: '16px' }}
                >
                  {selectedPolicy?.versionLabel || profile.policyVersionLabel}
                </button>
                {policyMenuOpen ? (
                  <div
                    role="listbox"
                    aria-label={`${profile.displayName}策略版本`}
                    className="absolute right-0 top-6 z-10 w-44 border border-[var(--trade-border)] bg-[var(--trade-panel-soft)] shadow-xl"
                  >
                    {policyOptions.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        role="option"
                        aria-selected={item.id === selectedPolicy?.id}
                        onClick={() => {
                          setSelectedPolicyId(item.id);
                          setPolicyMenuOpen(false);
                        }}
                        className={`block w-full px-2 py-1.5 text-left text-xs leading-4 transition-colors ${
                          item.id === selectedPolicy?.id
                            ? 'bg-[var(--trade-hover)]'
                            : 'text-[color:var(--trade-muted)] hover:bg-[var(--trade-hover)] hover:text-[color:var(--trade-fg)]'
                        }`}
                        style={{
                          fontSize: '12px',
                          lineHeight: '16px',
                          color: item.id === selectedPolicy?.id ? 'var(--trade-accent)' : undefined,
                          backgroundColor: item.id === selectedPolicy?.id ? 'var(--trade-hover)' : undefined,
                        }}
                      >
                        <span className="font-semibold">{item.versionLabel}</span>
                        {item.effectiveFrom ? <span className="ml-2 opacity-80">{item.effectiveFrom}</span> : null}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
            <section className="border border-[var(--trade-border)] bg-[var(--trade-panel-soft)] p-3">
              {selectedPolicy?.changeReason ? (
                <p className="mb-2 text-xs leading-5 text-[color:var(--trade-subtle)]">
                  调整原因：{selectedPolicy.changeReason}
                </p>
              ) : null}
              <p className="whitespace-pre-wrap text-sm leading-6 text-[color:var(--trade-readable)]">
                {selectedPolicy?.bodyMarkdown || profile.policyMarkdown || '暂无策略正文'}
              </p>
            </section>
          </div>

          <div className="mt-4">
            <HoldingsPanel positions={positions} symbolNames={run?.symbolNames} />
          </div>

          <div className="mt-4 border-t border-[var(--trade-border)] pt-4">
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-[color:var(--trade-fg)]">最新决策</h3>
              <span className="text-xs text-[color:var(--trade-subtle)]">{formatMaybeDateTime(decision?.decisionTime)}</span>
            </div>
            {decision ? (
              <div className="mt-3">
                <ActionBadge action={decision.action} symbol={decision.symbol} />
                <p className="mt-2 text-sm leading-6 text-[color:var(--trade-readable)]">{decision.rationale || '--'}</p>
                <p className="mt-2 text-xs text-[color:var(--trade-subtle)]">置信度 {formatConfidence(decision.confidence)}</p>
              </div>
            ) : (
              <p className="mt-3 text-sm text-[color:var(--trade-muted)]">暂无决策</p>
            )}
          </div>

          <section className="mt-4 border-t border-[var(--trade-border)] pt-4">
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-[color:var(--trade-fg)]">历史决策</h3>
              <span className="text-xs text-[color:var(--trade-subtle)]">{previousDecisions.length}</span>
            </div>
            {previousDecisions.length === 0 ? (
              <p className="mt-3 text-sm text-[color:var(--trade-muted)]">暂无更早决策</p>
            ) : (
              <ol className="mt-3 divide-y divide-[var(--trade-border)] border border-[var(--trade-border)]">
                {previousDecisions.map((item) => (
                  <li key={item.id} className="grid gap-3 bg-[var(--trade-panel-soft)] px-3 py-3 sm:grid-cols-[132px_minmax(0,1fr)]">
                    <div>
                      <p className="text-xs tabular-nums text-[color:var(--trade-subtle)]">{formatMaybeDateTime(item.decisionTime)}</p>
                      <p className="mt-1 text-xs text-[color:var(--trade-muted)]">
                        置信度 {formatConfidence(item.confidence)}
                      </p>
                    </div>
                    <div className="min-w-0">
                      <ActionBadge action={item.action} symbol={item.symbol} />
                      <p className="mt-1 text-sm leading-6 text-[color:var(--trade-readable)]">{item.rationale || '--'}</p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      </section>
      </div>
    </div>
  );
};

const AgentBacktestPage: React.FC = () => {
  useEffect(() => {
    document.title = '操盘';
  }, []);

  const [theme, setTheme] = useState<TradingTheme>(() => getStoredTheme());
  const [marketFilter, setMarketFilter] = useState<MarketFilter>('cn');
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [run, setRun] = useState<AgentBacktestRunItem | null>(null);
  const [events, setEvents] = useState<AgentBacktestEventsResponse | null>(null);
  const [profileFilter, setProfileFilter] = useState<ProfileFilter>('all');
  const [curveMode, setCurveMode] = useState<CurveMode>('return');
  const [isLoading, setIsLoading] = useState(false);
  const [selectedProfileId, setSelectedProfileId] = useState<number | null>(null);
  const [policyVersionsByProfileKey, setPolicyVersionsByProfileKey] = useState<Record<string, AgentBacktestPolicyItem[]>>({});
  const [expandedDecisionIds, setExpandedDecisionIds] = useState<Set<number>>(() => new Set());
  const [expandedTimelineIds, setExpandedTimelineIds] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  useEffect(() => {
    window.localStorage.setItem('trading-theme', theme);
  }, [theme]);

  const loadRuns = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const list = await agentBacktestApi.listRuns({ market: marketFilter, limit: 20 });
      setSelectedRunId(list.items[0]?.id ?? null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, [marketFilter]);

  const loadRunDetail = useCallback(async (runId: number) => {
    setIsLoading(true);
    setError(null);
    try {
      const [runDetail, runEvents] = await Promise.all([
        agentBacktestApi.getRun(runId),
        agentBacktestApi.listEvents(runId, { limit: 300 }),
      ]);
      setRun(runDetail);
      setEvents(runEvents);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    if (selectedRunId != null) {
      void loadRunDetail(selectedRunId);
    } else {
      setRun(null);
      setEvents(null);
    }
  }, [loadRunDetail, selectedRunId]);

  const profiles = useMemo(
    () => sortProfiles((run?.profiles || []).filter((profile) => profile.status === 'active')),
    [run?.profiles],
  );
  const profileById = useMemo(() => new Map(profiles.map((profile) => [profile.id, profile])), [profiles]);
  const activeProfileIds = useMemo(() => new Set(profiles.map((profile) => profile.id)), [profiles]);
  const stockPoolItems = useMemo<StockPoolItem[]>(
    () => (run?.symbols || []).map((symbol) => ({
      symbol,
      name: run?.symbolNames?.[symbol] || '',
    })),
    [run?.symbolNames, run?.symbols],
  );
  const latestNavMap = useMemo(() => latestNavByProfile(events?.dailyNav || []), [events?.dailyNav]);
  const latestDecisionMap = useMemo(() => latestDecisionByProfile(events?.decisions || []), [events?.decisions]);
  const selectedProfile = selectedProfileId == null ? null : profileById.get(selectedProfileId) ?? null;
  const selectedProfileIndex = useMemo(
    () => profiles.findIndex((profile) => profile.id === selectedProfileId),
    [profiles, selectedProfileId],
  );
  const selectedProfileNav = selectedProfile ? latestNavMap.get(selectedProfile.id) : undefined;
  const selectedProfileDecision = selectedProfile ? latestDecisionMap.get(selectedProfile.id) : undefined;
  const selectedProfileDecisionHistory = useMemo(
    () => (selectedProfile ? sortDecisionsDesc((events?.decisions || []).filter((item) => item.profileId === selectedProfile.id)) : []),
    [events?.decisions, selectedProfile],
  );
  const selectedProfilePolicies = selectedProfile ? policyVersionsByProfileKey[selectedProfile.profileKey] || [] : [];
  const curvePoints = useMemo(
    () => buildCurvePoints(events?.dailyNav || [], profiles, run?.initialCashPerAgent || 0, curveMode),
    [curveMode, events?.dailyNav, profiles, run?.initialCashPerAgent],
  );
  const timeline = useMemo(
    () => buildTimeline(events || EMPTY_EVENTS, run?.initialCashPerAgent || 0),
    [events, run?.initialCashPerAgent],
  );
  const filteredTimeline = useMemo(
    () => timeline
      .filter((item) => {
        if (!activeProfileIds.has(item.profileId)) return false;
        if (profileFilter === 'all') return true;
        return profileById.get(item.profileId)?.profileKey === profileFilter;
      })
      .slice(0, 16),
    [activeProfileIds, profileById, profileFilter, timeline],
  );
  const latestNavDate = useMemo(() => {
    const dates = (events?.dailyNav || []).map((item) => item.tradeDate).filter(Boolean) as string[];
    return dates.sort().at(-1) || '--';
  }, [events?.dailyNav]);
  const isReturnCurve = curveMode === 'return';
  const marketMeta = MARKET_META[marketFilter];
  const runMarketKey: MarketFilter = run?.market === 'us' ? 'us' : run?.market === 'cn' ? 'cn' : marketFilter;
  const runMarketMeta = MARKET_META[runMarketKey];
  const currencySymbol = getCurrencySymbol(run?.market || marketFilter);
  const selectAdjacentProfile = useCallback((direction: -1 | 1) => {
    if (selectedProfileIndex < 0 || profiles.length < 2) return;
    const nextIndex = (selectedProfileIndex + direction + profiles.length) % profiles.length;
    setSelectedProfileId(profiles[nextIndex].id);
  }, [profiles, selectedProfileIndex]);

  useEffect(() => {
    if (selectedProfileId != null && !profileById.has(selectedProfileId)) {
      setSelectedProfileId(null);
    }
  }, [profileById, selectedProfileId]);

  useEffect(() => {
    if (selectedProfileId == null) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelectedProfileId(null);
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        selectAdjacentProfile(-1);
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        selectAdjacentProfile(1);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectAdjacentProfile, selectedProfileId]);

  useEffect(() => {
    if (!selectedProfile || selectedRunId == null) return;
    if (policyVersionsByProfileKey[selectedProfile.profileKey]) return;
    let cancelled = false;
    agentBacktestApi.listPolicies(selectedRunId, selectedProfile.profileKey)
      .then((result) => {
        if (cancelled) return;
        setPolicyVersionsByProfileKey((current) => ({
          ...current,
          [selectedProfile.profileKey]: result.items,
        }));
      })
      .catch(() => {
        if (cancelled) return;
        setPolicyVersionsByProfileKey((current) => ({
          ...current,
          [selectedProfile.profileKey]: [],
        }));
      });
    return () => {
      cancelled = true;
    };
  }, [policyVersionsByProfileKey, selectedProfile, selectedRunId]);

  const refresh = () => {
    setFeedback(null);
    if (selectedRunId == null) {
      void loadRuns();
      return;
    }
    void loadRunDetail(selectedRunId);
  };

  return (
    <main
      style={{
        ...getThemeStyle(theme),
        backgroundColor: 'var(--trade-bg)',
        color: 'var(--trade-fg)',
      }}
      className="min-h-screen transition-colors duration-200"
    >
      <div className="mx-auto flex min-h-screen w-full max-w-[1440px] flex-col px-4 py-5 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-4 border-b border-[var(--trade-border)] pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold leading-tight sm:text-3xl" style={{ color: 'var(--trade-fg)' }}>操盘</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--trade-muted)]">
              {marketMeta.description}
            </p>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center lg:justify-end">
            <div className="inline-flex h-10 border border-[var(--trade-border)] bg-[var(--trade-panel)]">
              {(['cn', 'us'] as MarketFilter[]).map((market) => (
                <button
                  key={market}
                  type="button"
                  aria-pressed={marketFilter === market}
                  onClick={() => {
                    setMarketFilter(market);
                    setSelectedRunId(null);
                    setRun(null);
                    setEvents(null);
                    setProfileFilter('all');
                    setPolicyVersionsByProfileKey({});
                  }}
                  className={`inline-flex items-center px-3 text-sm transition-colors ${
                    marketFilter === market ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)] hover:bg-[var(--trade-hover)]'
                  }`}
                >
                  {MARKET_META[market].label}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={refresh}
              className="inline-flex h-10 items-center justify-center gap-2 border border-[var(--trade-border)] bg-[var(--trade-panel)] px-3 text-sm text-[var(--trade-fg)] transition-colors hover:bg-[var(--trade-hover)] disabled:cursor-not-allowed disabled:opacity-60"
              disabled={isLoading}
            >
              <RefreshCw className={isLoading ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
              刷新
            </button>
            <div className="inline-flex h-10 border border-[var(--trade-border)] bg-[var(--trade-panel)]">
              <button
                type="button"
                aria-pressed={theme === 'light'}
                onClick={() => setTheme('light')}
                className={`inline-flex items-center gap-2 px-3 text-sm transition-colors ${
                  theme === 'light' ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)]'
                }`}
              >
                <Sun className="h-4 w-4" />
                浅色
              </button>
              <button
                type="button"
                aria-pressed={theme === 'dark'}
                onClick={() => setTheme('dark')}
                className={`inline-flex items-center gap-2 px-3 text-sm transition-colors ${
                  theme === 'dark' ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)]'
                }`}
              >
                <Moon className="h-4 w-4" />
                深色
              </button>
            </div>
          </div>
        </header>

        {error ? (
          <div className="mt-4 border border-[var(--trade-negative)]/60 bg-[var(--trade-panel)] px-4 py-3 text-sm text-[var(--trade-negative)]">
            {error.message}
          </div>
        ) : null}
        {feedback ? (
          <div className="mt-4 border border-[var(--trade-positive)]/50 bg-[var(--trade-panel)] px-4 py-3 text-sm text-[var(--trade-positive)]">
            {feedback}
          </div>
        ) : null}

        <section className="grid flex-1 gap-5 py-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,0.75fr)]">
          <div className="min-w-0 space-y-5">
            <section className="border border-[var(--trade-border)] bg-[var(--trade-panel)]">
              <div className="flex flex-col gap-3 border-b border-[var(--trade-border)] px-4 py-4 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <h2 className="text-base font-semibold" style={{ color: 'var(--trade-fg)' }}>
                    {isReturnCurve ? '收益曲线' : '资金曲线'}
                  </h2>
                  <p className="mt-1 text-xs text-[var(--trade-muted)]">
                    最新净值日 {latestNavDate} · {isReturnCurve ? `基准资金 ${formatMoney(run?.initialCashPerAgent)} / 操盘手` : '按账户总权益展示'}
                  </p>
                </div>
                <div className="flex flex-col gap-3 sm:items-end">
                  <div className="grid w-full grid-cols-2 border border-[var(--trade-border)] text-xs sm:w-auto">
                    <button
                      type="button"
                      aria-pressed={curveMode === 'return'}
                      onClick={() => setCurveMode('return')}
                      className={`h-8 px-3 transition-colors ${
                        curveMode === 'return' ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)] hover:bg-[var(--trade-hover)]'
                      }`}
                    >
                      收益率
                    </button>
                    <button
                      type="button"
                      aria-pressed={curveMode === 'equity'}
                      onClick={() => setCurveMode('equity')}
                      className={`h-8 px-3 transition-colors ${
                        curveMode === 'equity' ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)] hover:bg-[var(--trade-hover)]'
                      }`}
                    >
                      账户权益
                    </button>
                  </div>
                </div>
              </div>

              <div className="h-[340px] px-2 py-4 sm:h-[420px]">
                {curvePoints.length === 0 ? (
                  <div className="flex h-full items-center justify-center text-sm text-[var(--trade-muted)]">
                    暂无每日净值快照
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                    <LineChart data={curvePoints} margin={{ top: 10, right: 24, bottom: 8, left: 0 }}>
                      <CartesianGrid stroke="var(--trade-grid)" vertical={false} />
                      <XAxis
                        dataKey="date"
                        tickFormatter={formatTradeDate}
                        stroke="var(--trade-subtle)"
                        tick={{ fill: 'var(--trade-muted)', fontSize: 12 }}
                        tickLine={false}
                        axisLine={{ stroke: 'var(--trade-border)' }}
                      />
                      <YAxis
                        tickFormatter={(value) => (
                          isReturnCurve ? `${Number(value).toFixed(1)}%` : formatCompactMoney(Number(value))
                        )}
                        stroke="var(--trade-subtle)"
                        tick={{ fill: 'var(--trade-muted)', fontSize: 12 }}
                        tickLine={false}
                        axisLine={false}
                        width={isReturnCurve ? 56 : 70}
                      />
                      <Tooltip
                        cursor={{ stroke: 'var(--trade-muted)', strokeDasharray: '4 4' }}
                        contentStyle={{
                          background: 'var(--trade-panel)',
                          border: '1px solid var(--trade-border)',
                          borderRadius: 0,
                          color: 'var(--trade-fg)',
                          boxShadow: 'none',
                        }}
                        formatter={(value, name) => [
                          isReturnCurve ? formatSignedPct(Number(value)) : `${currencySymbol}${formatMoney(Number(value))}`,
                          getProfileMeta(String(name)).shortLabel,
                        ]}
                        labelFormatter={(label) => `日期 ${label}`}
                      />
                      <Legend
                        verticalAlign="top"
                        align="right"
                        iconType="plainline"
                        formatter={(value) => (
                          <span className="text-xs text-[var(--trade-muted)]">
                            {getProfileMeta(String(value)).shortLabel}
                          </span>
                        )}
                      />
                      {profiles.map((profile) => {
                        const meta = getProfileMeta(profile.profileKey);
                        return (
                          <Line
                            key={profile.id}
                            type="monotone"
                            dataKey={profile.profileKey}
                            name={profile.profileKey}
                            stroke={meta.color}
                            strokeWidth={2}
                            dot={{ r: 3, strokeWidth: 1 }}
                            activeDot={{ r: 5 }}
                            connectNulls
                          />
                        );
                      })}
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            </section>

            <section
              className="grid gap-3"
              style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))' }}
            >
              {profiles.map((profile) => {
                const nav = latestNavMap.get(profile.id);
                const decision = latestDecisionMap.get(profile.id);
                const currentEquity = nav?.totalEquity ?? run?.initialCashPerAgent ?? null;
                const returnPct = nav ? getReturnPct(nav, run?.initialCashPerAgent || 0) : 0;
                const meta = getProfileMeta(profile.profileKey);
                return (
                  <article
                    key={profile.id}
                    role="button"
                    tabIndex={0}
                    aria-haspopup="dialog"
                    aria-label={`查看${profile.displayName}持仓明细`}
                    onClick={() => setSelectedProfileId(profile.id)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        setSelectedProfileId(profile.id);
                      }
                    }}
                    className="cursor-pointer border border-[var(--trade-border)] bg-[var(--trade-panel)] p-4 outline-none transition-colors hover:bg-[var(--trade-hover)] focus-visible:ring-1 focus-visible:ring-[var(--trade-muted)]"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold" style={{ color: 'var(--trade-fg)' }}>{profile.displayName}</h3>
                        <p className="mt-1 text-xs leading-5 text-[var(--trade-subtle)]">
                          {meta.shortLabel} · {profile.status} · 加入 {getProfileJoinedAt(profile)}
                        </p>
                      </div>
                      <span className="h-2.5 w-2.5 shrink-0" style={{ backgroundColor: meta.color }} />
                    </div>

                    <div className="mt-5 grid grid-cols-2 gap-4">
                      <div>
                        <p className="text-xs text-[var(--trade-muted)]">当前收益</p>
                        <p
                          className="mt-1 text-xl font-semibold tabular-nums"
                          style={{ color: (returnPct ?? 0) >= 0 ? 'var(--trade-positive)' : 'var(--trade-negative)' }}
                        >
                          {formatSignedPct(returnPct)}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-[var(--trade-muted)]">当前权益</p>
                        <p className="mt-1 text-lg font-semibold tabular-nums">{formatMoney(currentEquity)}</p>
                      </div>
                      <div>
                        <p className="text-xs text-[var(--trade-muted)]">策略版本</p>
                        <p className="mt-1 text-sm font-medium">{profile.policyVersionLabel}</p>
                      </div>
                      <div>
                        <p className="text-xs text-[var(--trade-muted)]">持仓数</p>
                        <p className="mt-1 text-sm font-medium tabular-nums">{positionCount(nav)}</p>
                      </div>
                    </div>

	                    <div className="mt-5 border-t border-[var(--trade-border)] pt-4">
                      <div className="flex items-center justify-between gap-3">
                        <p className="text-xs text-[var(--trade-muted)]">最新决策</p>
                        <span className="text-xs text-[var(--trade-subtle)]">{formatMaybeDateTime(decision?.decisionTime)}</span>
                      </div>
	                      {decision ? (
	                        <div className="mt-2">
	                          <ActionBadge action={decision.action} symbol={decision.symbol} />
	                          <div className="mt-1">
	                            <ExpandableText
	                              text={decision.rationale}
	                              expanded={expandedDecisionIds.has(decision.id)}
	                              onToggle={() => toggleNumberSet(setExpandedDecisionIds, decision.id)}
	                              className="text-sm leading-6 text-[var(--trade-readable)]"
	                              ariaLabel={`${profile.displayName}最新决策详情`}
	                            />
	                          </div>
	                          <div className="mt-2 flex items-center justify-between gap-3">
	                            <p className="text-xs text-[var(--trade-subtle)]">
	                              置信度 {formatConfidence(decision.confidence)}
	                            </p>
	                          </div>
	                        </div>
                      ) : (
                        <p className="mt-2 text-sm text-[var(--trade-muted)]">暂无决策</p>
	                      )}
	                    </div>
	                  </article>
                );
              })}
            </section>
          </div>

          <aside className="min-w-0 space-y-5">
            <section className="border border-[var(--trade-border)] bg-[var(--trade-panel)]">
              <div className="border-b border-[var(--trade-border)] px-4 py-4">
                <h2 className="text-base font-semibold" style={{ color: 'var(--trade-fg)' }}>实验概览</h2>
                <p className="mt-1 text-xs text-[var(--trade-muted)]">{run?.name || '暂无实验'}</p>
              </div>
              <dl className="grid grid-cols-2 gap-px bg-[var(--trade-border)] text-sm">
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">股票池</dt>
                  <dd className="mt-1 font-medium">{run?.symbols.length ?? 0} 只 {runMarketMeta.label}</dd>
                </div>
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">规则</dt>
                  <dd className="mt-1 font-medium">{run?.ruleVersion || '--'}</dd>
                </div>
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">决策次数</dt>
                  <dd className="mt-1 font-medium">{run?.maxObservationsPerDay ?? '--'} / 日</dd>
                </div>
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">状态</dt>
                  <dd className="mt-1 font-medium">{run?.status || '--'}</dd>
                </div>
              </dl>
              <div className="border-t border-[var(--trade-border)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-xs font-medium text-[var(--trade-fg)]">观察股票池</h3>
                  <span className="text-xs text-[var(--trade-subtle)]">{stockPoolItems.length}</span>
                </div>
                {stockPoolItems.length === 0 ? (
                  <p className="mt-4 text-sm text-[var(--trade-muted)]">暂无股票</p>
                ) : (
                  <ul className="mt-3 max-h-[340px] space-y-2 overflow-y-auto pr-1">
                    {stockPoolItems.map((item) => (
                      <li
                        key={item.symbol}
                        className="grid grid-cols-[82px_minmax(0,1fr)] items-center gap-3 border border-[var(--trade-border)] bg-[var(--trade-panel-soft)] px-3 py-2"
                      >
                        <span className="font-mono text-sm tabular-nums text-[var(--trade-fg)]">{item.symbol}</span>
                        <span className="truncate text-sm text-[var(--trade-readable)]">{item.name || '名称待补'}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>

            <section className="border border-[var(--trade-border)] bg-[var(--trade-panel)]">
              <div className="border-b border-[var(--trade-border)] px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold" style={{ color: 'var(--trade-fg)' }}>事件</h2>
                    <p className="mt-1 text-xs text-[var(--trade-muted)]">观察、决策、订单、成交、净值按时间排列。</p>
                  </div>
                  <span className="text-xs text-[var(--trade-subtle)]">{filteredTimeline.length}</span>
                </div>
                <div
                  className="mt-4 grid border border-[var(--trade-border)] text-xs"
                  style={{ gridTemplateColumns: `repeat(${profiles.length + 1}, minmax(0, 1fr))` }}
                >
                  <button
                    type="button"
                    onClick={() => setProfileFilter('all')}
                    className={`h-8 transition-colors ${profileFilter === 'all' ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)] hover:bg-[var(--trade-hover)]'}`}
                  >
                    全部
                  </button>
                  {profiles.map((profile) => {
                    const meta = getProfileMeta(profile.profileKey);
                    return (
                      <button
                        key={profile.id}
                        type="button"
                        onClick={() => setProfileFilter(profile.profileKey)}
                        className={`h-8 transition-colors ${profileFilter === profile.profileKey ? 'bg-[var(--trade-fg)] text-[var(--trade-bg)]' : 'text-[var(--trade-muted)] hover:bg-[var(--trade-hover)]'}`}
                      >
                        {meta.shortLabel}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="max-h-[680px] overflow-y-auto">
                {!run && !isLoading ? (
                  <p className="px-4 py-12 text-center text-sm text-[var(--trade-muted)]">暂无操盘实验</p>
                ) : filteredTimeline.length === 0 ? (
                  <p className="px-4 py-12 text-center text-sm text-[var(--trade-muted)]">暂无事件</p>
                ) : (
                  <ol className="divide-y divide-[var(--trade-border)]">
                    {filteredTimeline.map((item) => {
                      const profile = profileById.get(item.profileId);
                      const meta = getProfileMeta(profile?.profileKey || '');
                      return (
                        <li key={item.id} className="grid grid-cols-[88px_minmax(0,1fr)] gap-3 px-4 py-3">
                          <div>
                            <p className="text-xs tabular-nums text-[var(--trade-subtle)]">{formatMaybeDateTime(item.at)}</p>
                            <p className="mt-2 inline-flex border border-[var(--trade-border)] px-1.5 py-0.5 text-[11px] text-[var(--trade-muted)]">
                              {typeLabel(item.type)}
                            </p>
                          </div>
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="h-2 w-2 shrink-0" style={{ backgroundColor: meta.color }} />
                              <p className="truncate text-sm font-medium">{item.title}</p>
                              {item.type === 'decision' ? <ActionBadge action={item.action} /> : null}
                            </div>
                            <p className="mt-1 text-xs text-[var(--trade-subtle)]">{getProfileLabel(profile)}</p>
                            <div className="mt-2">
                              <ExpandableText
                                text={item.detail}
                                expanded={expandedTimelineIds.has(item.id)}
                                onToggle={() => toggleStringSet(setExpandedTimelineIds, item.id)}
                                className="text-sm leading-6 text-[var(--trade-readable)]"
                                ariaLabel={`${item.title}详情`}
                              />
                            </div>
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </div>
            </section>
          </aside>
        </section>
      </div>
      {selectedProfile ? (
        <ProfileDetailDialog
          key={selectedProfile.id}
          profile={selectedProfile}
          profileIndex={selectedProfileIndex}
          profileCount={profiles.length}
          nav={selectedProfileNav}
          decision={selectedProfileDecision}
          decisionHistory={selectedProfileDecisionHistory}
          policyVersions={selectedProfilePolicies}
          run={run}
          onPreviousProfile={() => selectAdjacentProfile(-1)}
          onNextProfile={() => selectAdjacentProfile(1)}
          onClose={() => setSelectedProfileId(null)}
        />
      ) : null}
    </main>
  );
};

export default AgentBacktestPage;
