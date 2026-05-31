import type React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentBacktestPage from '../AgentBacktestPage';

const {
  mockGetRun,
  mockListEvents,
  mockListPolicies,
  mockListRuns,
} = vi.hoisted(() => ({
  mockGetRun: vi.fn(),
  mockListEvents: vi.fn(),
  mockListPolicies: vi.fn(),
  mockListRuns: vi.fn(),
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
    listPolicies: mockListPolicies,
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
    createdAt: '2026-05-27T11:40:32',
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
    createdAt: '2026-05-27T11:40:33',
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
    createdAt: '2026-05-27T11:40:34',
  },
];

const run = {
  id: 1,
  name: 'A股三周期纸面交易实验',
  status: 'draft',
  market: 'cn',
  symbols: ['002975', '002222'],
  symbolNames: {
    '002975': '博杰股份',
    '002222': '福晶科技',
    '603267': '鸿远电子',
  },
  startDate: '2026-05-27',
  endDate: null,
  initialCashPerAgent: 20000,
  maxObservationsPerDay: 3,
  ruleVersion: 'cn_a_v1',
  config: {},
  profiles,
};

const usRun = {
  ...run,
  id: 9,
  name: '美股现金账户实验',
  market: 'us',
  symbols: ['AAPL', 'NVDA'],
  symbolNames: {
    AAPL: 'Apple Inc.',
    NVDA: 'NVIDIA Corp.',
  },
  initialCashPerAgent: 1000,
  ruleVersion: 'us_cash_ibkr_v1',
  config: {
    baseCurrency: 'USD',
    cashSettlement: 'T+1',
  },
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
      rationale: '验证日不交易，短线账户继续等待更清晰的放量确认；如果盘中高低点区间仍然收窄，就保持观察，不主动追高。',
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
    {
      id: 3,
      runId: 1,
      profileId: 1,
      observationId: null,
      tradeDate: '2026-05-26',
      decisionTime: '2026-05-26T14:40:00',
      action: 'hold',
      symbol: '603267',
      side: null,
      quantity: null,
      orderType: null,
      limitPrice: null,
      confidence: 0.51,
      rationale: '昨日试探买入条件未满足，继续观察鸿远电子尾盘强度。',
      riskNotes: null,
      policyVersionLabel: 'v1.0-short',
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
      payload: {
        positions: [
          {
            symbol: '603267',
            quantity: 200,
            avgCost: 68.1656814,
            totalCost: 13633.13628,
            lastPrice: 70.06,
            marketValueBase: 14012,
            unrealizedPnlBase: 378.86372,
            valuationDate: '2026-05-28',
            valuationStale: false,
          },
        ],
      },
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
  mockListPolicies.mockResolvedValue({
    items: [
      {
        id: 2,
        runId: 1,
        profileId: 1,
        versionLabel: 'v1.0-short',
        bodyMarkdown: '短线策略',
        parentPolicyId: 1,
        effectiveFrom: '2026-05-27',
        changeReason: null,
        status: 'active',
        createdAt: '2026-05-27T11:40:32',
      },
      {
        id: 1,
        runId: 1,
        profileId: 1,
        versionLabel: 'v0.9-short',
        bodyMarkdown: '旧版短线策略',
        parentPolicyId: null,
        effectiveFrom: '2026-05-26',
        changeReason: '初始试运行',
        status: 'active',
        createdAt: '2026-05-26T11:40:32',
      },
    ],
    total: 2,
  });
});

describe('AgentBacktestPage', () => {
  it('renders the standalone trading page with return chart and profile cards', async () => {
    render(<AgentBacktestPage />);

    expect(await screen.findByRole('heading', { name: '操盘' })).toBeInTheDocument();
    expect(screen.getByText('收益曲线')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '收益率' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '账户权益' })).toBeInTheDocument();
    expect(await screen.findByTestId('return-chart')).toBeInTheDocument();
    expect(screen.getByTestId('line-short')).toBeInTheDocument();
    expect(screen.getByTestId('line-medium')).toBeInTheDocument();
    expect(screen.getByTestId('line-long')).toBeInTheDocument();
    expect(screen.getAllByText('当前权益')).toHaveLength(3);
    expect(screen.getAllByText('策略版本')).toHaveLength(3);
    expect(screen.getAllByText(/加入/)).toHaveLength(3);
    expect(screen.getByText('v1.0-short')).toBeInTheDocument();
    expect(screen.getByText('+0.60%')).toBeInTheDocument();
    expect(screen.getAllByText(/验证日不交易/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '记录收盘净值' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存设置' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('选择操盘实验')).not.toBeInTheDocument();
    expect(screen.getByText('博杰股份')).toBeInTheDocument();
    expect(screen.getByText('福晶科技')).toBeInTheDocument();

    await waitFor(() => {
      expect(mockListRuns).toHaveBeenCalledWith({ market: 'cn', limit: 20 });
      expect(mockListEvents).toHaveBeenCalledWith(1, { limit: 300 });
    });
  });

  it('filters experiments by A-share or US market', async () => {
    mockListRuns
      .mockResolvedValueOnce({ items: [run], total: 1 })
      .mockResolvedValueOnce({ items: [usRun], total: 1 });
    mockGetRun
      .mockResolvedValueOnce(run)
      .mockResolvedValueOnce(usRun);

    render(<AgentBacktestPage />);

    await waitFor(() => {
      expect(mockListRuns).toHaveBeenCalledWith({ market: 'cn', limit: 20 });
    });

    fireEvent.click(screen.getByRole('button', { name: '美股' }));

    await waitFor(() => {
      expect(mockListRuns).toHaveBeenCalledWith({ market: 'us', limit: 20 });
      expect(mockGetRun).toHaveBeenCalledWith(9);
    });
    expect((await screen.findAllByText(/美股现金账户实验/)).length).toBeGreaterThan(0);
    expect(screen.getByText('Apple Inc.')).toBeInTheDocument();
    expect(screen.getByText('NVIDIA Corp.')).toBeInTheDocument();
  });

  it('switches between return and account equity curves', async () => {
    render(<AgentBacktestPage />);

    await screen.findByText('收益曲线');
    fireEvent.click(screen.getByRole('button', { name: '账户权益' }));

    expect(screen.getByText('资金曲线')).toBeInTheDocument();
    expect(screen.getByText(/按账户总权益展示/)).toBeInTheDocument();
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

  it('renders the stock pool as read-only code and name rows', async () => {
    render(<AgentBacktestPage />);

    expect(await screen.findByText('观察股票池')).toBeInTheDocument();
    expect(await screen.findByText('002975')).toBeInTheDocument();
    expect(screen.getByText('博杰股份')).toBeInTheDocument();
    expect(screen.getByText('002222')).toBeInTheDocument();
    expect(screen.getByText('福晶科技')).toBeInTheDocument();
    expect(screen.queryByLabelText('观察股票池')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('每日允许决策次数')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存设置' })).not.toBeInTheDocument();
  });

  it('does not render explicit expand or collapse buttons for long text', async () => {
    render(<AgentBacktestPage />);

    await screen.findAllByText(/验证日不交易/);

    expect(screen.queryByRole('button', { name: '展开' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '收起' })).not.toBeInTheDocument();
  });

  it('opens a centered trader detail dialog with current holdings', async () => {
    render(<AgentBacktestPage />);

    const shortCard = await screen.findByRole('button', { name: '查看短线操盘手持仓明细' });
    fireEvent.click(shortCard);

    const dialog = await screen.findByRole('dialog', { name: '短线操盘手' });
    const card = within(dialog);
    expect(card.getByText('持仓明细')).toBeInTheDocument();
    expect(card.getAllByText('603267').length).toBeGreaterThan(0);
    expect(card.getByText('鸿远电子')).toBeInTheDocument();
    expect(card.getByText('建仓均价')).toBeInTheDocument();
    expect(card.getByText('当前价')).toBeInTheDocument();
    expect(card.getByText('涨跌幅')).toBeInTheDocument();
    expect(card.getByText('+2.78%')).toBeInTheDocument();
    expect(card.getByText('68.166')).toBeInTheDocument();
    expect(card.getByText('70.06')).toBeInTheDocument();
    expect(card.getByText('13,633.14')).toBeInTheDocument();
    expect(card.getByText('378.86')).toBeInTheDocument();
    expect(card.getByText('历史决策')).toBeInTheDocument();
    expect(card.getByText(/昨日试探买入条件未满足/)).toBeInTheDocument();
    expect(card.getByText('当前策略')).toBeInTheDocument();
    expect(card.getByText('短线策略')).toBeInTheDocument();
    expect(card.queryByRole('button', { name: '查看短线操盘手策略' })).not.toBeInTheDocument();
    expect(card.getAllByText('持有').length).toBeGreaterThan(0);

    const versionButton = await card.findByRole('button', { name: '切换短线操盘手策略版本' });
    fireEvent.click(versionButton);
    fireEvent.click(card.getByRole('option', { name: /v0.9-short/ }));
    expect(card.getByText('旧版短线策略')).toBeInTheDocument();
    expect(mockListPolicies).toHaveBeenCalledWith(1, 'short');

    fireEvent.click(card.getByRole('button', { name: '关闭持仓明细' }));
    expect(screen.queryByRole('dialog', { name: '短线操盘手' })).not.toBeInTheDocument();
  });

  it('switches trader detail dialogs with side controls and arrow keys', async () => {
    render(<AgentBacktestPage />);

    fireEvent.click(await screen.findByRole('button', { name: '查看短线操盘手持仓明细' }));
    expect(await screen.findByRole('dialog', { name: '短线操盘手' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '下一位操盘手' }));
    expect(await screen.findByRole('dialog', { name: '中线操盘手' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '短线操盘手' })).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(await screen.findByRole('dialog', { name: '长线操盘手' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '上一位操盘手' }));
    expect(await screen.findByRole('dialog', { name: '中线操盘手' })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(await screen.findByRole('dialog', { name: '短线操盘手' })).toBeInTheDocument();
  });
});
