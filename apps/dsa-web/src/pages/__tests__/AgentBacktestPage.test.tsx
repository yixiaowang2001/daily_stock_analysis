import type React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentBacktestPage from '../AgentBacktestPage';

const {
  mockGetRun,
  mockListEvents,
  mockListRuns,
  mockRecordDailyNav,
} = vi.hoisted(() => ({
  mockGetRun: vi.fn(),
  mockListEvents: vi.fn(),
  mockListRuns: vi.fn(),
  mockRecordDailyNav: vi.fn(),
}));

vi.mock('recharts', () => ({
  CartesianGrid: () => <g data-testid="chart-grid" />,
  Legend: () => <div data-testid="chart-legend" />,
  Line: ({ dataKey }: { dataKey: string }) => <path data-testid={`line-${dataKey}`} />,
  LineChart: ({ children }: { children: React.ReactNode }) => <svg data-testid="return-chart">{children}</svg>,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Tooltip: () => <div data-testid="chart-tooltip" />,
  XAxis: () => <g data-testid="chart-x-axis" />,
  YAxis: () => <g data-testid="chart-y-axis" />,
}));

vi.mock('../../api/agentBacktest', () => ({
  agentBacktestApi: {
    listRuns: mockListRuns,
    getRun: mockGetRun,
    listEvents: mockListEvents,
    recordDailyNav: mockRecordDailyNav,
  },
}));

const profiles = [
  {
    id: 1,
    runId: 1,
    accountId: 1,
    profileKey: 'short',
    displayName: '短线操盘手',
    styleProfile: 'short',
    policyVersionLabel: 'v1.0-short',
    policyMarkdown: '短线策略',
    latestPolicyId: 1,
    contextNamespace: 'agent_backtest:1:short',
    status: 'active',
  },
  {
    id: 2,
    runId: 1,
    accountId: 2,
    profileKey: 'medium',
    displayName: '中线操盘手',
    styleProfile: 'medium',
    policyVersionLabel: 'v1.0-medium',
    policyMarkdown: '中线策略',
    latestPolicyId: 2,
    contextNamespace: 'agent_backtest:1:medium',
    status: 'active',
  },
  {
    id: 3,
    runId: 1,
    accountId: 3,
    profileKey: 'long',
    displayName: '长线操盘手',
    styleProfile: 'long',
    policyVersionLabel: 'v1.0-long',
    policyMarkdown: '长线策略',
    latestPolicyId: 3,
    contextNamespace: 'agent_backtest:1:long',
    status: 'active',
  },
];

const run = {
  id: 1,
  name: 'A股三周期纸面交易实验',
  status: 'draft',
  market: 'cn',
  symbols: ['002975', '002222'],
  startDate: '2026-05-27',
  endDate: null,
  initialCashPerAgent: 20000,
  maxObservationsPerDay: 3,
  ruleVersion: 'cn_a_v1',
  config: {},
  profiles,
};

const events = {
  observations: [
    {
      id: 1,
      runId: 1,
      profileId: 1,
      tradeDate: '2026-05-27',
      observationTime: '09:40:00',
      dataCutoffAt: '2026-05-27T09:40:00',
      sequenceNo: 1,
      symbols: ['002975', '002222'],
      evidence: {},
      summary: 'morning context generated',
    },
  ],
  decisions: [
    {
      id: 1,
      runId: 1,
      profileId: 1,
      observationId: 1,
      tradeDate: '2026-05-27',
      decisionTime: '2026-05-27T09:43:00',
      action: 'hold',
      symbol: null,
      side: null,
      quantity: null,
      orderType: null,
      limitPrice: null,
      confidence: 0.55,
      rationale: '验证日不交易',
      riskNotes: '等待正式定时',
      policyVersionLabel: 'v1.0-short',
      rawOutput: {},
    },
    {
      id: 2,
      runId: 1,
      profileId: 2,
      observationId: null,
      tradeDate: '2026-05-27',
      decisionTime: '2026-05-27T13:30:00',
      action: 'hold',
      symbol: null,
      side: null,
      quantity: null,
      orderType: null,
      limitPrice: null,
      confidence: 0.62,
      rationale: '中线继续等待',
      riskNotes: null,
      policyVersionLabel: 'v1.0-medium',
      rawOutput: {},
    },
  ],
  orders: [],
  fills: [],
  dailyNav: [
    {
      id: 1,
      runId: 1,
      profileId: 1,
      tradeDate: '2026-05-27',
      cash: 20120,
      marketValue: 0,
      totalEquity: 20120,
      realizedPnl: 120,
      unrealizedPnl: 0,
      payload: { positions: [] },
    },
    {
      id: 2,
      runId: 1,
      profileId: 2,
      tradeDate: '2026-05-27',
      cash: 20000,
      marketValue: 0,
      totalEquity: 20000,
      realizedPnl: 0,
      unrealizedPnl: 0,
      payload: { positions: [] },
    },
    {
      id: 3,
      runId: 1,
      profileId: 3,
      tradeDate: '2026-05-27',
      cash: 19860,
      marketValue: 0,
      totalEquity: 19860,
      realizedPnl: -140,
      unrealizedPnl: 0,
      payload: { positions: [] },
    },
  ],
};

const storage = new Map<string, string>();

beforeEach(() => {
  storage.clear();
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
      clear: () => storage.clear(),
    },
  });
  vi.clearAllMocks();
  mockListRuns.mockResolvedValue({ items: [run], total: 1 });
  mockGetRun.mockResolvedValue(run);
  mockListEvents.mockResolvedValue(events);
  mockRecordDailyNav.mockResolvedValue({ runId: 1, tradeDate: '2026-05-27', items: events.dailyNav });
});

describe('AgentBacktestPage', () => {
  it('renders the standalone trading page with return chart and profile cards', async () => {
    render(<AgentBacktestPage />);

    expect(await screen.findByRole('heading', { name: '操盘' })).toBeInTheDocument();
    expect(screen.getByText('收益曲线')).toBeInTheDocument();
    expect(await screen.findByTestId('return-chart')).toBeInTheDocument();
    expect(screen.getByTestId('line-short')).toBeInTheDocument();
    expect(screen.getByTestId('line-medium')).toBeInTheDocument();
    expect(screen.getByTestId('line-long')).toBeInTheDocument();
    expect(screen.getAllByText('当前权益')).toHaveLength(3);
    expect(screen.getAllByText('策略版本')).toHaveLength(3);
    expect(screen.getByText('v1.0-short')).toBeInTheDocument();
    expect(screen.getByText('+0.60%')).toBeInTheDocument();
    expect(screen.getAllByText('验证日不交易').length).toBeGreaterThan(0);

    await waitFor(() => {
      expect(mockListEvents).toHaveBeenCalledWith(1, { limit: 300 });
    });
  });

  it('filters the event stream locally by profile key', async () => {
    render(<AgentBacktestPage />);

    await screen.findByText('morning context generated');
    fireEvent.click(screen.getByRole('button', { name: '中线' }));

    expect(screen.queryByText('morning context generated')).not.toBeInTheDocument();
    expect(screen.getAllByText('中线继续等待').length).toBeGreaterThan(0);
    expect(mockListEvents).toHaveBeenCalledTimes(1);
  });

  it('persists the page theme choice', async () => {
    render(<AgentBacktestPage />);

    await screen.findByText('收益曲线');
    fireEvent.click(screen.getByRole('button', { name: '浅色' }));

    expect(window.localStorage.getItem('trading-theme')).toBe('light');
  });

  it('records daily nav from the standalone action', async () => {
    render(<AgentBacktestPage />);

    await screen.findByText('收益曲线');
    fireEvent.click(screen.getByRole('button', { name: '记录收盘净值' }));

    await waitFor(() => {
      expect(mockRecordDailyNav).toHaveBeenCalledWith(1, expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/));
    });
    expect(await screen.findByText('已记录 2026-05-27 的 3 个操盘手净值快照。')).toBeInTheDocument();
  });
});
