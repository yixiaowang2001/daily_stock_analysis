import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Moon, RefreshCw, Sun } from 'lucide-react';
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
import { toDateInputValue } from '../utils/format';
import type {
  AgentBacktestDailyNavItem,
  AgentBacktestDecisionItem,
  AgentBacktestEventsResponse,
  AgentBacktestProfileItem,
  AgentBacktestRunItem,
} from '../types/agentBacktest';

type ProfileFilter = 'all' | string;
type TradingTheme = 'light' | 'dark';
type TimelineType = 'observation' | 'decision' | 'order' | 'fill' | 'nav';

type TimelineItem = {
  id: string;
  at: string;
  type: TimelineType;
  profileId: number;
  title: string;
  detail: string;
};

type ReturnPoint = {
  date: string;
  short?: number;
  medium?: number;
  long?: number;
  [key: string]: number | string | undefined;
};

type ThemeStyle = React.CSSProperties & Record<string, string>;

const PROFILE_ORDER = ['short', 'medium', 'long'];
const PROFILE_META: Record<string, { label: string; color: string; shortLabel: string }> = {
  short: { label: '短线操盘手', color: '#2563eb', shortLabel: '短线' },
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

function todayIso(): string {
  return toDateInputValue(new Date());
}

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
      '--trade-muted': '#a2a9b0',
      '--trade-subtle': '#737b84',
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

function getReturnPct(nav: AgentBacktestDailyNavItem | undefined, initialCash: number): number | null {
  if (!nav || !initialCash) return null;
  return ((nav.totalEquity - initialCash) / initialCash) * 100;
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

function buildReturnPoints(
  navs: AgentBacktestDailyNavItem[],
  profiles: AgentBacktestProfileItem[],
  initialCash: number,
): ReturnPoint[] {
  const profileById = new Map(profiles.map((profile) => [profile.id, profile]));
  const byDate = new Map<string, ReturnPoint>();

  for (const item of navs) {
    const profile = profileById.get(item.profileId);
    if (!profile || !item.tradeDate || !initialCash) continue;
    const point = byDate.get(item.tradeDate) || { date: item.tradeDate };
    point[profile.profileKey] = Number((((item.totalEquity - initialCash) / initialCash) * 100).toFixed(4));
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
    title: `决策 ${item.action}`,
    detail: [item.symbol, item.rationale].filter(Boolean).join(' · ') || item.policyVersionLabel,
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

const AgentBacktestPage: React.FC = () => {
  useEffect(() => {
    document.title = '操盘';
  }, []);

  const [theme, setTheme] = useState<TradingTheme>(() => getStoredTheme());
  const [runs, setRuns] = useState<AgentBacktestRunItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [run, setRun] = useState<AgentBacktestRunItem | null>(null);
  const [events, setEvents] = useState<AgentBacktestEventsResponse | null>(null);
  const [profileFilter, setProfileFilter] = useState<ProfileFilter>('all');
  const [navDate, setNavDate] = useState(todayIso());
  const [isLoading, setIsLoading] = useState(false);
  const [isRecordingNav, setIsRecordingNav] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  useEffect(() => {
    window.localStorage.setItem('trading-theme', theme);
  }, [theme]);

  const loadRuns = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const list = await agentBacktestApi.listRuns({ limit: 20 });
      setRuns(list.items);
      setSelectedRunId((current) => current ?? list.items[0]?.id ?? null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, []);

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
    }
  }, [loadRunDetail, selectedRunId]);

  const profiles = useMemo(() => sortProfiles(run?.profiles || []), [run?.profiles]);
  const profileById = useMemo(() => new Map(profiles.map((profile) => [profile.id, profile])), [profiles]);
  const latestNavMap = useMemo(() => latestNavByProfile(events?.dailyNav || []), [events?.dailyNav]);
  const latestDecisionMap = useMemo(() => latestDecisionByProfile(events?.decisions || []), [events?.decisions]);
  const returnPoints = useMemo(
    () => buildReturnPoints(events?.dailyNav || [], profiles, run?.initialCashPerAgent || 0),
    [events?.dailyNav, profiles, run?.initialCashPerAgent],
  );
  const timeline = useMemo(
    () => buildTimeline(events || EMPTY_EVENTS, run?.initialCashPerAgent || 0),
    [events, run?.initialCashPerAgent],
  );
  const filteredTimeline = useMemo(
    () => timeline
      .filter((item) => {
        if (profileFilter === 'all') return true;
        return profileById.get(item.profileId)?.profileKey === profileFilter;
      })
      .slice(0, 16),
    [profileById, profileFilter, timeline],
  );
  const latestNavDate = useMemo(() => {
    const dates = (events?.dailyNav || []).map((item) => item.tradeDate).filter(Boolean) as string[];
    return dates.sort().at(-1) || '--';
  }, [events?.dailyNav]);

  const refresh = () => {
    setFeedback(null);
    if (selectedRunId == null) {
      void loadRuns();
      return;
    }
    void loadRunDetail(selectedRunId);
  };

  const recordDailyNav = async () => {
    if (selectedRunId == null) return;
    setIsRecordingNav(true);
    setFeedback(null);
    setError(null);
    try {
      const response = await agentBacktestApi.recordDailyNav(selectedRunId, navDate);
      setFeedback(`已记录 ${response.tradeDate} 的 ${response.items.length} 个操盘手净值快照。`);
      await loadRunDetail(selectedRunId);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsRecordingNav(false);
    }
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
              三个隔离操盘手的收益率对比。每日 16:00 后用净值快照计算收益曲线。
            </p>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center lg:justify-end">
            <select
              aria-label="选择操盘实验"
              value={selectedRunId ?? ''}
              onChange={(event) => {
                const next = Number(event.target.value);
                setSelectedRunId(Number.isFinite(next) ? next : null);
                setProfileFilter('all');
              }}
              className="h-10 min-w-[220px] rounded-none border border-[var(--trade-border)] bg-[var(--trade-panel)] px-3 text-sm text-[var(--trade-fg)] outline-none transition-colors focus:border-[var(--trade-muted)]"
            >
              {runs.length === 0 ? <option value="">暂无实验</option> : null}
              {runs.map((item) => (
                <option key={item.id} value={item.id}>
                  #{item.id} {item.name}
                </option>
              ))}
            </select>
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
                  <h2 className="text-base font-semibold" style={{ color: 'var(--trade-fg)' }}>收益曲线</h2>
                  <p className="mt-1 text-xs text-[var(--trade-muted)]">
                    最新净值日 {latestNavDate} · 基准资金 {formatMoney(run?.initialCashPerAgent)} / 操盘手
                  </p>
                </div>
                <div className="flex items-center gap-2 text-xs text-[var(--trade-subtle)]">
                  <span className="h-2 w-2 bg-[#2563eb]" />
                  <span>短线</span>
                  <span className="ml-2 h-2 w-2 bg-[#d97706]" />
                  <span>中线</span>
                  <span className="ml-2 h-2 w-2 bg-[#059669]" />
                  <span>长线</span>
                </div>
              </div>

              <div className="h-[340px] px-2 py-4 sm:h-[420px]">
                {returnPoints.length === 0 ? (
                  <div className="flex h-full items-center justify-center text-sm text-[var(--trade-muted)]">
                    暂无每日净值快照
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={returnPoints} margin={{ top: 10, right: 24, bottom: 8, left: 0 }}>
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
                        tickFormatter={(value) => `${Number(value).toFixed(1)}%`}
                        stroke="var(--trade-subtle)"
                        tick={{ fill: 'var(--trade-muted)', fontSize: 12 }}
                        tickLine={false}
                        axisLine={false}
                        width={56}
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
                          formatSignedPct(Number(value)),
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

            <section className="grid gap-3 lg:grid-cols-3">
              {profiles.map((profile) => {
                const nav = latestNavMap.get(profile.id);
                const decision = latestDecisionMap.get(profile.id);
                const returnPct = getReturnPct(nav, run?.initialCashPerAgent || 0);
                const meta = getProfileMeta(profile.profileKey);
                return (
                  <article
                    key={profile.id}
                    className="border border-[var(--trade-border)] bg-[var(--trade-panel)] p-4"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-semibold" style={{ color: 'var(--trade-fg)' }}>{profile.displayName}</h3>
                        <p className="mt-1 text-xs text-[var(--trade-subtle)]">{meta.shortLabel} · {profile.status}</p>
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
                        <p className="mt-1 text-lg font-semibold tabular-nums">{formatMoney(nav?.totalEquity)}</p>
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
                          <p className="text-sm font-medium">{actionLabel(decision.action)} {decision.symbol || ''}</p>
                          <p className="mt-1 line-clamp-2 text-sm leading-6 text-[var(--trade-muted)]">
                            {decision.rationale || '--'}
                          </p>
                          <p className="mt-2 text-xs text-[var(--trade-subtle)]">
                            置信度 {formatConfidence(decision.confidence)}
                          </p>
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
                <h2 className="text-base font-semibold" style={{ color: 'var(--trade-fg)' }}>运行</h2>
                <p className="mt-1 text-xs text-[var(--trade-muted)]">{run?.name || '暂无实验'}</p>
              </div>
              <dl className="grid grid-cols-2 gap-px bg-[var(--trade-border)] text-sm">
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">股票池</dt>
                  <dd className="mt-1 font-medium">{run?.symbols.length ?? 0} 只 A 股</dd>
                </div>
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">规则</dt>
                  <dd className="mt-1 font-medium">{run?.ruleVersion || '--'}</dd>
                </div>
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">观察次数</dt>
                  <dd className="mt-1 font-medium">{run?.maxObservationsPerDay ?? '--'} / 日</dd>
                </div>
                <div className="bg-[var(--trade-panel)] p-4">
                  <dt className="text-xs text-[var(--trade-muted)]">状态</dt>
                  <dd className="mt-1 font-medium">{run?.status || '--'}</dd>
                </div>
              </dl>
              <div className="flex flex-col gap-2 border-t border-[var(--trade-border)] p-4 sm:flex-row sm:flex-wrap">
                <input
                  aria-label="净值日期"
                  type="date"
                  value={navDate}
                  onChange={(event) => setNavDate(event.target.value)}
                  className="h-10 min-w-0 flex-1 border border-[var(--trade-border)] bg-[var(--trade-panel-soft)] px-3 text-sm text-[var(--trade-fg)] outline-none focus:border-[var(--trade-muted)] sm:min-w-[170px]"
                />
                <button
                  type="button"
                  onClick={() => void recordDailyNav()}
                  disabled={isRecordingNav || selectedRunId == null}
                  title="只记录收盘后净值快照，不触发买卖决策"
                  className="h-10 shrink-0 whitespace-nowrap border border-[var(--trade-border)] bg-[var(--trade-fg)] px-3 text-sm font-medium text-[var(--trade-bg)] transition-opacity disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isRecordingNav ? '记录中' : '记录收盘净值'}
                </button>
                <p className="w-full text-xs leading-5 text-[var(--trade-muted)]">
                  只写入选定日期的净值快照，用来更新收益曲线，不触发买卖；通常 16:00 后使用。
                </p>
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
                <div className="mt-4 grid grid-cols-4 border border-[var(--trade-border)] text-xs">
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
                            </div>
                            <p className="mt-1 text-xs text-[var(--trade-subtle)]">{getProfileLabel(profile)}</p>
                            <p className="mt-2 line-clamp-2 text-sm leading-6 text-[var(--trade-muted)]">{item.detail}</p>
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
    </main>
  );
};

export default AgentBacktestPage;
