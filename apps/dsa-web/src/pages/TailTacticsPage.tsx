import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { agentApi, type ChatStreamRequest } from '../api/agent';
import { tailTacticsApi, type TailExperiment, type TailStrategyVersion } from '../api/tailTactics';
import { ApiErrorAlert, Button, Card, ConfirmDialog, Input, ModalDialog, PageHeader, ScrollArea, SectionCard } from '../components/common';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { cn } from '../utils/cn';

type TabKey = 'strategy_cases' | 'experiment';

const DEFAULT_TAIL_STRATEGY_BODY = `# 尾盘 T+1 双层策略 v1.4

## 定位
- 本策略明确分为两层：第一层是用户在同花顺等工具中使用的「尾盘选股策略」，第二层是 Agent 对候选做的「评估/预测策略」。
- 第一层只负责生成候选池，不直接给出买入/卖出结论。
- 第二层只负责对候选池逐票评分、风险闸门、次日开盘预测和复盘，不修改用户当天在同花顺里的筛选条件，除非复盘结论明确建议迭代第一层规则。

## 第一层：尾盘选股策略（用户 / 同花顺动态选股）
- 一句话选股：主板，涨幅 3%-7%，近 20 日有涨停，量比 > 1，换手率 5%-10%，总市值 50 亿-200 亿，股价运行于当日分时均价线上且强于大盘，个股创当日新高且回踩分时均线不破。
- 结构化条件：
  1. 上市板块是主板。
  2. 涨跌幅 >= 3% 且 <= 7%。
  3. 近 20 日存在涨停或明显涨停基因。
  4. 量比 > 1。
  5. 换手率 >= 5% 且 <= 10%。
  6. 总市值 >= 50 亿且 <= 200 亿。
  7. 行情价格运行在当日分时均价线上。
  8. 个股强于大盘 / 跑赢大盘。
  9. 个股创当日新高后回踩分时均线不破。
- 这一层的输出是候选池。不要把 Agent 的 watch/candidate/priority、开盘预测、风险闸门扣分混入同花顺筛选条件。

## 第二层：Agent 评估 / 预测策略
- 只评估第一层已经筛出的候选，不负责临场扩展新股票。
- 风险闸门优先于评分；环境不合适或数据不足时允许全部降级为观察或拒绝。
- watch 表示只观察，不推荐；candidate 表示进入次日观察池；priority 只代表研究优先级最高，不等于买入建议。

## 尾盘节奏
- 14:30 初筛：判断指数、情绪、板块是否允许继续研究。
- 14:40 评分截点：只使用此刻及之前可观察的数据给每只候选打分。
- 14:55 确认：仅做执行前风控复核，不把 14:40 之后发生的行情回填进评分。

## 评分口径
- 市场环境 15 分：指数、情绪、亏钱效应、北向/成交额等。
- 板块强度 20 分：主线持续性、涨停梯队、前排反馈。
- 个股量价 25 分：日 K 位置、分时承接、放量质量、均线结构。
- 流动性 15 分：成交额、换手、盘口可交易性。
- 催化质量 15 分：新闻、公告、产业事件与资金共识。
- 风险闸门最多扣 30 分：ST/退市/停牌/新股异常/尾盘直线拉升/公告不确定等。

## 输出纪律
- action_level 只能是 reject / watch / candidate / priority。
- priority 只代表研究优先级最高，不等于投资建议。
- 候选池只有一只也不强制推荐；低质量候选必须给 reject 或 watch。
- 每只候选必须写明 risk_flags、next_day_watch、next_open_forecast 和 invalidation_conditions。
- 复盘建议必须标明影响的是第一层同花顺选股规则，还是第二层 Agent 评估/预测规则。`;

function parseStockCodes(raw: string): string[] {
  const parts = raw.split(/[\s,，;；\n\t]+/).map((s) => s.trim()).filter(Boolean);
  const uniq: string[] = [];
  for (const p of parts) {
    if (!uniq.includes(p)) uniq.push(p);
    if (uniq.length >= 10) break;
  }
  return uniq;
}

function buildExperimentParamSnapshot(args: {
  riskMode: string;
  marketContext: string;
  holdingPeriod: string;
  exclusions: string;
}): Record<string, unknown> {
  return {
    workflow_goal: 'conversation_first_tail_pick',
    agent_role: 'A股尾盘候选池逐票评分与复盘助手',
    risk_mode: args.riskMode,
    intended_holding_period: args.holdingPeriod,
    data_cutoff_time: '14:40',
    timing_windows: ['14:30 初筛', '14:40 评分截点', '14:55 收盘前风控复核'],
    score_weights: {
      market: 15,
      sector: 20,
      price_volume: 25,
      liquidity: 15,
      catalyst: 15,
      risk_gate: -30,
    },
    hard_exclusions: args.exclusions
      .split(/[\n,，;；]+/)
      .map((s) => s.trim())
      .filter(Boolean),
    user_market_context: args.marketContext.trim() || null,
    output_contract: {
      required_root_key: 'tail_score_result',
      required_fields: [
        'code',
        'score',
        'action_level',
        'confidence',
        'factor_scores',
        'risk_flags',
        'data_quality_flags',
        'next_day_watch',
        'next_open_forecast',
        'invalidation_conditions',
        'one_line_reason',
      ],
    },
  };
}

async function consumeTailAgentStream(
  payload: ChatStreamRequest,
  signal: AbortSignal,
  onEvent: (line: string) => void,
): Promise<{ content: string; sessionId: string }> {
  const response = await agentApi.chatStream(payload, {
    signal,
    headers: { 'X-DSA-Tail-Ranking': '1' },
  });
  if (!response.ok || !response.body) {
    const t = await response.text().catch(() => '');
    throw new Error(t || `HTTP ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let finalContent = '';
  let sessionId = payload.session_id || '';
  const processLine = (line: string) => {
    if (!line.startsWith('data: ')) return;
    const event = JSON.parse(line.slice(6)) as Record<string, unknown>;
    onEvent(line);
    if (event.type === 'done') {
      if (event.success === false) {
        throw new Error(String(event.error || event.content || 'Agent failed'));
      }
      finalContent = String(event.content ?? '');
      if (typeof event.session_id === 'string') sessionId = event.session_id;
    }
    if (event.type === 'error') {
      throw new Error(String(event.message || 'Stream error'));
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop() ?? '';
    for (const line of lines) {
      if (line.trim()) processLine(line.trim());
    }
  }
  if (buf.trim().startsWith('data: ')) processLine(buf.trim());
  return { content: finalContent, sessionId };
}

const TailTacticsPage: React.FC = () => {
  const [tab, setTab] = useState<TabKey>('strategy_cases');
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [versions, setVersions] = useState<TailStrategyVersion[]>([]);
  const [experiments, setExperiments] = useState<TailExperiment[]>([]);
  const [loading, setLoading] = useState(false);

  const [newVersionLabel, setNewVersionLabel] = useState('v1.0');
  const [newVersionTitle, setNewVersionTitle] = useState('尾盘 T+1 双层策略');
  const [newVersionBody, setNewVersionBody] = useState(DEFAULT_TAIL_STRATEGY_BODY);
  const [newVersionParent, setNewVersionParent] = useState<string>('');

  const [diffA, setDiffA] = useState<string>('');
  const [diffB, setDiffB] = useState<string>('');
  const [diffText, setDiffText] = useState('');

  const [expTradeDate, setExpTradeDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [expStrategyId, setExpStrategyId] = useState<string>('');
  const [expPaste, setExpPaste] = useState('');
  const [expRiskMode, setExpRiskMode] = useState('strict');
  const [expHoldingPeriod, setExpHoldingPeriod] = useState('T+1 / 次日早盘复盘');
  const [expMarketContext, setExpMarketContext] = useState('');
  const [expExclusions, setExpExclusions] = useState(
    'ST/*ST、退市整理、停牌/临停、上市前5日新股、成交额过低、尾盘直线拉升无承接',
  );

  const [selectedExpId, setSelectedExpId] = useState<number | null>(null);
  const [selectedExp, setSelectedExp] = useState<TailExperiment | null>(null);
  const [morningRows, setMorningRows] = useState<Record<string, string>>({});
  const [morningTradeDateOverride, setMorningTradeDateOverride] = useState('');
  const [morningFetchNotes, setMorningFetchNotes] = useState<string[]>([]);
  const [morningFetchBusy, setMorningFetchBusy] = useState(false);
  const [streamPreview, setStreamPreview] = useState('');
  const [streamLines, setStreamLines] = useState<string[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [reviewNote, setReviewNote] = useState('');
  const [caseSummary, setCaseSummary] = useState('');
  const [deleteConfirmExp, setDeleteConfirmExp] = useState<TailExperiment | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [viewStrategy, setViewStrategy] = useState<TailStrategyVersion | null>(null);
  const [viewExperimentModal, setViewExperimentModal] = useState<TailExperiment | null>(null);
  const [viewExperimentLoading, setViewExperimentLoading] = useState(false);
  const [deleteConfirmVersion, setDeleteConfirmVersion] = useState<TailStrategyVersion | null>(null);
  const [deleteVersionBusy, setDeleteVersionBusy] = useState(false);

  const refreshVersions = useCallback(async () => {
    const v = await tailTacticsApi.listVersions();
    setVersions(v);
    if (!expStrategyId && v.length > 0) {
      setExpStrategyId(String(v[0].id));
    }
  }, [expStrategyId]);

  const refreshExperiments = useCallback(async () => {
    const e = await tailTacticsApi.listExperiments({ limit: 100 });
    setExperiments(e);
  }, []);

  const confirmDeleteExperiment = useCallback(async () => {
    if (!deleteConfirmExp || deleteBusy) return;
    const id = deleteConfirmExp.id;
    setDeleteBusy(true);
    setError(null);
    try {
      await tailTacticsApi.deleteExperiment(id);
      setViewExperimentModal((cur) => (cur?.id === id ? null : cur));
      setSelectedExpId((cur) => (cur === id ? null : cur));
      await refreshExperiments();
      setDeleteConfirmExp(null);
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    } finally {
      setDeleteBusy(false);
    }
  }, [deleteBusy, deleteConfirmExp, refreshExperiments]);

  const confirmDeleteVersion = useCallback(async () => {
    if (!deleteConfirmVersion || deleteVersionBusy) return;
    const vid = deleteConfirmVersion.id;
    setDeleteVersionBusy(true);
    setError(null);
    try {
      await tailTacticsApi.deleteVersion(vid);
      setViewStrategy((cur) => (cur?.id === vid ? null : cur));
      setDeleteConfirmVersion(null);
      const v = await tailTacticsApi.listVersions(100);
      setVersions(v);
      setExpStrategyId((cur) => (cur === String(vid) ? (v.length ? String(v[0].id) : '') : cur));
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    } finally {
      setDeleteVersionBusy(false);
    }
  }, [deleteConfirmVersion, deleteVersionBusy]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([refreshVersions(), refreshExperiments()]);
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    } finally {
      setLoading(false);
    }
  }, [refreshExperiments, refreshVersions]);

  useEffect(() => {
    document.title = '尾盘战术台 - DSA';
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (selectedExpId == null) {
      setSelectedExp(null);
      setMorningFetchNotes([]);
      return;
    }
    setMorningFetchNotes([]);
    let cancelled = false;
    void (async () => {
      try {
        const ex = await tailTacticsApi.getExperiment(selectedExpId);
        if (!cancelled) {
          setSelectedExp(ex);
          setReviewNote(ex.review_note_markdown || '');
          setCaseSummary(ex.case_summary || '');
          const metrics = await tailTacticsApi.getMorningMetrics(selectedExpId).catch(() => []);
          const mr: Record<string, string> = {};
          for (const s of ex.symbols) {
            const found = metrics.find((m) => m.symbol === s);
            mr[s] = found?.surge_pct_prev_close_930_1000 != null ? String(found.surge_pct_prev_close_930_1000) : '';
          }
          setMorningRows(mr);
        }
      } catch (e: unknown) {
        if (!cancelled) setError(getParsedApiError(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedExpId]);

  const parsedSymbols = useMemo(() => parseStockCodes(expPaste), [expPaste]);
  const experimentParamSnapshot = useMemo(
    () =>
      buildExperimentParamSnapshot({
        riskMode: expRiskMode,
        holdingPeriod: expHoldingPeriod,
        marketContext: expMarketContext,
        exclusions: expExclusions,
      }),
    [expExclusions, expHoldingPeriod, expMarketContext, expRiskMode],
  );

  const handleCreateVersion = async () => {
    setError(null);
    try {
      await tailTacticsApi.createVersion({
        version_label: newVersionLabel,
        title: newVersionTitle,
        body_markdown: newVersionBody || '（待补充）',
        parent_version_id: newVersionParent ? Number(newVersionParent) : undefined,
      });
      setNewVersionBody(DEFAULT_TAIL_STRATEGY_BODY);
      await refreshVersions();
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    }
  };

  const handleDiff = async () => {
    if (!diffA || !diffB || diffA === diffB) return;
    setError(null);
    try {
      const d = await tailTacticsApi.diffVersions(Number(diffA), Number(diffB));
      setDiffText(d.unified_diff || '（无差异输出）');
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    }
  };

  const handleCreateExperiment = async () => {
    if (!expStrategyId) {
      setError(getParsedApiError(new Error('请选择策略版本')));
      return;
    }
    const symbols = parsedSymbols;
    if (symbols.length === 0) {
      setError(getParsedApiError(new Error('请粘贴至少一只股票代码')));
      return;
    }
    setError(null);
    try {
      await tailTacticsApi.createExperiment({
        trade_date: expTradeDate,
        strategy_version_id: Number(expStrategyId),
        pasted_raw: expPaste,
        symbols,
        param_snapshot: experimentParamSnapshot,
      });
      setExpPaste('');
      await refreshExperiments();
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    }
  };

  const runRankStream = async () => {
    if (!selectedExp) return;
    setError(null);
    setStreamPreview('');
    setStreamLines([]);
    setStreaming(true);
    const ac = new AbortController();
    try {
      const compose = await tailTacticsApi.compose(selectedExp.id, 'rank');
      const payload: ChatStreamRequest = {
        message: compose.message,
        session_id: compose.session_id,
        context: compose.context,
      };
      const { content, sessionId } = await consumeTailAgentStream(payload, ac.signal, (line) => {
        setStreamLines((prev) => (prev.length > 200 ? prev : [...prev, line]));
      });
      setStreamPreview(content);
      await tailTacticsApi.patchExperiment(selectedExp.id, {
        ranking_output: content,
        ranking_session_id: sessionId,
        status: 'ranked',
      });
      await refreshExperiments();
      const ex = await tailTacticsApi.getExperiment(selectedExp.id);
      setSelectedExp(ex);
    } catch (e: unknown) {
      if ((e as Error).name !== 'AbortError') setError(getParsedApiError(e));
    } finally {
      setStreaming(false);
    }
  };

  const runReviewStream = async () => {
    if (!selectedExp) return;
    setError(null);
    setStreamPreview('');
    setStreamLines([]);
    setStreaming(true);
    const ac = new AbortController();
    try {
      const compose = await tailTacticsApi.compose(selectedExp.id, 'review');
      const payload: ChatStreamRequest = {
        message: compose.message,
        session_id: compose.session_id,
        context: compose.context,
      };
      const { content } = await consumeTailAgentStream(payload, ac.signal, (line) => {
        setStreamLines((prev) => (prev.length > 200 ? prev : [...prev, line]));
      });
      setStreamPreview(content);
      await tailTacticsApi.patchExperiment(selectedExp.id, {
        review_note_markdown: content,
      });
      setReviewNote(content);
      await refreshExperiments();
    } catch (e: unknown) {
      if ((e as Error).name !== 'AbortError') setError(getParsedApiError(e));
    } finally {
      setStreaming(false);
    }
  };

  const saveMorningMetrics = async () => {
    if (!selectedExp) return;
    setError(null);
    try {
      const items = selectedExp.symbols.map((symbol) => ({
        symbol,
        surge_pct_prev_close_930_1000:
          morningRows[symbol] === '' ? undefined : Number(morningRows[symbol]),
      }));
      await tailTacticsApi.putMorningMetrics(selectedExp.id, items);
      const metrics = await tailTacticsApi.getMorningMetrics(selectedExp.id);
      const mr: Record<string, string> = {};
      for (const s of selectedExp.symbols) {
        const found = metrics.find((m) => m.symbol === s);
        mr[s] = found?.surge_pct_prev_close_930_1000 != null ? String(found.surge_pct_prev_close_930_1000) : '';
      }
      setMorningRows(mr);
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    }
  };

  const handleAutoFetchMorning = async () => {
    if (!selectedExp) return;
    setError(null);
    setMorningFetchBusy(true);
    try {
      const payload =
        morningTradeDateOverride.trim() !== '' ? { morning_trade_date: morningTradeDateOverride.trim() } : {};
      const out = await tailTacticsApi.autoFetchMorningMetrics(selectedExp.id, payload);
      setMorningFetchNotes(out.notes);
      const mr: Record<string, string> = {};
      for (const row of out.items) {
        mr[row.symbol] =
          row.surge_pct_prev_close_930_1000 != null ? String(row.surge_pct_prev_close_930_1000) : '';
      }
      setMorningRows(mr);
      const ex = await tailTacticsApi.getExperiment(selectedExp.id);
      setSelectedExp(ex);
    } catch (e: unknown) {
      setMorningFetchNotes([]);
      setError(getParsedApiError(e));
    } finally {
      setMorningFetchBusy(false);
    }
  };

  const saveCaseMeta = async () => {
    if (!selectedExp) return;
    setError(null);
    try {
      await tailTacticsApi.patchExperiment(selectedExp.id, {
        review_note_markdown: reviewNote || null,
        case_summary: caseSummary || null,
      });
      await refreshExperiments();
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    }
  };

  const openExperimentDetailModal = async (row: TailExperiment) => {
    setViewExperimentLoading(true);
    setError(null);
    try {
      const full = await tailTacticsApi.getExperiment(row.id);
      setViewExperimentModal(full);
    } catch (e: unknown) {
      setError(getParsedApiError(e));
    } finally {
      setViewExperimentLoading(false);
    }
  };

  const tabs: { key: TabKey; label: string }[] = [
    { key: 'strategy_cases', label: '策略与案例' },
    { key: 'experiment', label: '实验与评分' },
  ];

  const fieldClass = 'flex flex-col gap-2';
  const fieldLabelClass = 'text-xs font-medium leading-snug text-secondary-text';
  const controlClass =
    'mt-0 w-full rounded-md border border-input bg-background px-3 py-2.5 text-sm leading-normal shadow-sm';

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6 p-4 md:p-8">
      <PageHeader
        title="尾盘战术台"
        description="配置策略版本、登记候选池与次日早盘冲高，使用 Agent 做逐票评分与复盘（非投资建议）。"
      />

      {error ? (
        <div className="pb-1">
          <ApiErrorAlert error={error} />
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-3 pt-1">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            className={cn(
              'rounded-lg px-4 py-2 text-sm font-medium leading-normal transition-colors',
              tab === t.key
                ? 'bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))]'
                : 'text-secondary-text hover:bg-muted',
            )}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
        <Button variant="secondary" size="sm" className="ml-auto" onClick={() => void loadAll()} disabled={loading}>
          刷新
        </Button>
      </div>

      {tab === 'strategy_cases' ? (
        <div className="space-y-6">
          <div className="grid gap-6 lg:grid-cols-2">
            <SectionCard title="新建策略版本">
              <div className="flex flex-col gap-5">
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className={fieldClass}>
                    <span className={fieldLabelClass}>版本标签</span>
                    <Input value={newVersionLabel} onChange={(e) => setNewVersionLabel(e.target.value)} />
                  </div>
                  <div className={fieldClass}>
                    <span className={fieldLabelClass}>标题</span>
                    <Input value={newVersionTitle} onChange={(e) => setNewVersionTitle(e.target.value)} />
                  </div>
                </div>
                <div className={fieldClass}>
                  <span className={fieldLabelClass}>父版本（可选）</span>
                  <select
                    className={controlClass}
                    value={newVersionParent}
                    onChange={(e) => setNewVersionParent(e.target.value)}
                  >
                    <option value="">无</option>
                    {versions.map((v) => (
                      <option key={v.id} value={String(v.id)}>
                        {v.version_label} — {v.title}
                      </option>
                    ))}
                  </select>
                </div>
                <div className={fieldClass}>
                  <span className={fieldLabelClass}>策略正文（Markdown）</span>
                  <textarea
                    className={`${controlClass} min-h-[200px] resize-y font-mono`}
                    value={newVersionBody}
                    onChange={(e) => setNewVersionBody(e.target.value)}
                  />
                </div>
                <Button onClick={() => void handleCreateVersion()}>保存新版本</Button>
              </div>
            </SectionCard>

            <SectionCard title="案例库（实验）">
              <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
                在「实验与评分」中运行评分与复盘；此处可查看详情或删除。删除后不可恢复。
              </p>
              <ScrollArea className="max-h-[400px] rounded-lg border border-border" viewportClassName="p-1">
                <table className="w-full border-separate border-spacing-0 text-left text-sm">
                  <thead>
                    <tr className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
                      <th className="px-3 py-3 font-medium">id</th>
                      <th className="px-3 py-3 font-medium">日期</th>
                      <th className="px-3 py-3 font-medium">标的</th>
                      <th className="px-3 py-3 font-medium">状态</th>
                      <th className="px-3 py-3 font-medium">摘要</th>
                      <th className="px-3 py-3 font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {experiments.map((ex) => (
                      <tr key={ex.id} className="border-b border-border/60">
                        <td className="px-3 py-3 font-mono leading-relaxed">{ex.id}</td>
                        <td className="px-3 py-3 leading-relaxed">{ex.trade_date}</td>
                        <td className="max-w-[180px] truncate px-3 py-3 font-mono text-xs leading-relaxed">
                          {ex.symbols.join(',')}
                        </td>
                        <td className="px-3 py-3 leading-relaxed">{ex.status}</td>
                        <td className="max-w-[140px] truncate px-3 py-3 text-xs leading-relaxed text-muted-foreground">
                          {ex.case_summary || '—'}
                        </td>
                        <td className="px-3 py-3">
                          <div className="flex flex-wrap gap-2">
                            <Button
                              type="button"
                              variant="secondary"
                              size="sm"
                              onClick={() => void openExperimentDetailModal(ex)}
                            >
                              查看
                            </Button>
                            <Button type="button" variant="danger-subtle" size="sm" onClick={() => setDeleteConfirmExp(ex)}>
                              删除
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollArea>
            </SectionCard>
          </div>

          <SectionCard title="策略版本列表与对比">
            <div className="flex flex-col gap-5">
              <ScrollArea className="max-h-[320px] rounded-lg border border-border" viewportClassName="p-1">
                <table className="w-full border-separate border-spacing-0 text-left text-sm">
                  <thead>
                    <tr className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
                      <th className="px-3 py-3 font-medium">版本</th>
                      <th className="px-3 py-3 font-medium">标题</th>
                      <th className="px-3 py-3 font-medium">id</th>
                      <th className="px-3 py-3 font-medium">父版本</th>
                      <th className="px-3 py-3 font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.map((v) => (
                      <tr key={v.id} className="border-b border-border/60">
                        <td className="px-3 py-3 font-medium leading-relaxed">{v.version_label}</td>
                        <td className="px-3 py-3 text-secondary-text leading-relaxed">{v.title}</td>
                        <td className="px-3 py-3 font-mono text-xs leading-relaxed">{v.id}</td>
                        <td className="px-3 py-3 font-mono text-xs leading-relaxed">{v.parent_version_id ?? '—'}</td>
                        <td className="px-3 py-3">
                          <div className="flex flex-wrap gap-2">
                            <Button type="button" variant="secondary" size="sm" onClick={() => setViewStrategy(v)}>
                              查看
                            </Button>
                            <Button type="button" variant="danger-subtle" size="sm" onClick={() => setDeleteConfirmVersion(v)}>
                              删除
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollArea>
              <div className="border-t border-border pt-5">
                <span className={fieldLabelClass}>版本对比</span>
                <div className="mt-3 flex flex-wrap items-end gap-4">
                  <div className={fieldClass}>
                    <span className={fieldLabelClass}>版本 A</span>
                    <select
                      className={`${controlClass} min-w-[180px]`}
                      value={diffA}
                      onChange={(e) => setDiffA(e.target.value)}
                    >
                      <option value="">选择</option>
                      {versions.map((v) => (
                        <option key={v.id} value={String(v.id)}>
                          {v.version_label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className={fieldClass}>
                    <span className={fieldLabelClass}>版本 B</span>
                    <select
                      className={`${controlClass} min-w-[180px]`}
                      value={diffB}
                      onChange={(e) => setDiffB(e.target.value)}
                    >
                      <option value="">选择</option>
                      {versions.map((v) => (
                        <option key={v.id} value={String(v.id)}>
                          {v.version_label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <Button variant="secondary" className="shrink-0" onClick={() => void handleDiff()}>
                    生成 unified diff
                  </Button>
                </div>
              </div>
              {diffText ? (
                <pre className="mt-2 max-h-[320px] overflow-auto rounded-lg border border-border bg-muted/40 p-4 text-xs leading-relaxed">
                  {diffText}
                </pre>
              ) : null}
            </div>
          </SectionCard>
        </div>
      ) : null}

      {tab === 'experiment' ? (
        <div className="grid gap-6 xl:grid-cols-2">
          <SectionCard title="新建实验">
            <div className="flex flex-col gap-5 text-sm">
              <div className={fieldClass}>
                <span className={fieldLabelClass}>交易日 T</span>
                <Input type="date" value={expTradeDate} onChange={(e) => setExpTradeDate(e.target.value)} />
              </div>
              <div className={fieldClass}>
                <span className={fieldLabelClass}>策略版本</span>
                <select
                  className={controlClass}
                  value={expStrategyId}
                  onChange={(e) => setExpStrategyId(e.target.value)}
                >
                  {versions.map((v) => (
                    <option key={v.id} value={String(v.id)}>
                      {v.version_label} — {v.title}
                    </option>
                  ))}
                </select>
              </div>
              <div className={fieldClass}>
                <span className={fieldLabelClass}>粘贴候选池（最多 10 只，逗号/空格/换行分隔）</span>
                <textarea
                  className={`${controlClass} min-h-[120px] resize-y`}
                  value={expPaste}
                  onChange={(e) => setExpPaste(e.target.value)}
                />
              </div>
              <p className="-mt-1 rounded-md bg-muted/30 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                解析预览：{parsedSymbols.join(', ') || '（空）'}
              </p>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className={fieldClass}>
                  <span className={fieldLabelClass}>风控模式</span>
                  <select className={controlClass} value={expRiskMode} onChange={(e) => setExpRiskMode(e.target.value)}>
                    <option value="strict">严格：风险闸门优先</option>
                    <option value="balanced">均衡：机会与风险同权</option>
                    <option value="aggressive">进攻：允许更高波动</option>
                  </select>
                </div>
                <div className={fieldClass}>
                  <span className={fieldLabelClass}>持有/验证周期</span>
                  <Input value={expHoldingPeriod} onChange={(e) => setExpHoldingPeriod(e.target.value)} />
                </div>
              </div>
              <div className={fieldClass}>
                <span className={fieldLabelClass}>今日市场上下文（可选）</span>
                <textarea
                  className={`${controlClass} min-h-[80px] resize-y`}
                  value={expMarketContext}
                  onChange={(e) => setExpMarketContext(e.target.value)}
                />
              </div>
              <div className={fieldClass}>
                <span className={fieldLabelClass}>硬排除条件</span>
                <textarea
                  className={`${controlClass} min-h-[72px] resize-y`}
                  value={expExclusions}
                  onChange={(e) => setExpExclusions(e.target.value)}
                />
              </div>
              <details className="rounded-md border border-border bg-muted/20 px-3 py-2 text-xs">
                <summary className="cursor-pointer font-medium text-muted-foreground">对话快照预览</summary>
                <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap leading-relaxed text-muted-foreground">
                  {JSON.stringify(experimentParamSnapshot, null, 2)}
                </pre>
              </details>
              <Button onClick={() => void handleCreateExperiment()}>创建实验</Button>
            </div>
          </SectionCard>

          <SectionCard title="实验详情与 Agent">
            <div className={fieldClass}>
              <span className={fieldLabelClass}>选择实验</span>
              <select
                className={controlClass}
                value={selectedExpId != null ? String(selectedExpId) : ''}
                onChange={(e) => setSelectedExpId(e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">请选择</option>
                {experiments.map((ex) => (
                  <option key={ex.id} value={String(ex.id)}>
                    #{ex.id} {ex.trade_date} ({ex.symbols.join(',')}) — {ex.status}
                  </option>
                ))}
              </select>
            </div>

            {selectedExp ? (
              <div className="mt-6 space-y-6 border-t border-border pt-6">
                <Card className="space-y-3 p-4 text-sm leading-relaxed">
                  <p>
                    <span className="text-muted-foreground">策略版本 id：</span>
                    {selectedExp.strategy_version_id}
                  </p>
                  <p>
                    <span className="text-muted-foreground">状态：</span>
                    {selectedExp.status}
                  </p>
                  <div className="pt-1">
                    <span className="text-xs font-medium text-muted-foreground">粘贴原文</span>
                    <pre className="mt-2 max-h-52 overflow-auto rounded-md border border-border bg-muted/30 p-3 text-xs leading-relaxed">
                      {selectedExp.pasted_raw}
                    </pre>
                  </div>
                  {selectedExp.param_snapshot ? (
                    <div className="pt-1">
                      <span className="text-xs font-medium text-muted-foreground">对话/参数快照</span>
                      <pre className="mt-2 max-h-52 overflow-auto rounded-md border border-border bg-muted/30 p-3 text-xs leading-relaxed">
                        {JSON.stringify(selectedExp.param_snapshot, null, 2)}
                      </pre>
                    </div>
                  ) : null}
                </Card>

                <div className="flex flex-wrap gap-3">
                  <Button onClick={() => void runRankStream()} disabled={streaming}>
                    发起评分（流式 Agent）
                  </Button>
                  <Button variant="secondary" onClick={() => void runReviewStream()} disabled={streaming}>
                    生成复盘（需已准备早盘指标）
                  </Button>
                  <Button variant="danger-subtle" size="sm" onClick={() => setDeleteConfirmExp(selectedExp)} disabled={streaming}>
                    删除本实验
                  </Button>
                </div>

                <div>
                  <h4 className="mb-3 text-sm font-medium leading-snug">次日早盘冲高（相对昨收 %）</h4>
                  <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
                    默认按实验「交易日 T」之后<strong>首个 A 股交易日</strong>，用东方财富 5 分钟 K（9:30–10:01）最高价相对
                    <strong>前一交易日收盘</strong>
                    计算冲高幅度；分钟数据缺失时回退为当日<strong>日线最高价</strong>（口径更宽，见每条 source）。需联网。
                  </p>
                  <div className={fieldClass}>
                    <span className={fieldLabelClass}>覆盖早盘数据日（可选，YYYY-MM-DD）</span>
                    <Input
                      type="date"
                      className="max-w-[220px]"
                      value={morningTradeDateOverride}
                      onChange={(e) => setMorningTradeDateOverride(e.target.value)}
                    />
                  </div>
                  <div className="mt-3 flex flex-wrap gap-3">
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      disabled={streaming || morningFetchBusy}
                      onClick={() => void handleAutoFetchMorning()}
                    >
                      {morningFetchBusy ? '拉取中…' : '自动拉取并保存'}
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => void saveMorningMetrics()}>
                      手动保存（微调数值）
                    </Button>
                  </div>
                  {morningFetchNotes.length > 0 ? (
                    <ul className="mt-3 list-inside list-disc rounded-md border border-border/60 bg-muted/20 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                      {morningFetchNotes.map((n, i) => (
                        <li key={`${i}-${n}`}>{n}</li>
                      ))}
                    </ul>
                  ) : null}
                  <div className="mt-4 space-y-3 rounded-lg border border-border bg-muted/10 p-4">
                    {selectedExp.symbols.map((sym) => (
                      <div key={sym} className="flex flex-wrap items-center gap-3">
                        <span className="min-w-[5.5rem] shrink-0 font-mono text-sm leading-normal">{sym}</span>
                        <Input
                          type="number"
                          step="0.01"
                          className="max-w-[160px]"
                          value={morningRows[sym] ?? ''}
                          onChange={(e) => setMorningRows((r) => ({ ...r, [sym]: e.target.value }))}
                          placeholder="自动或手填"
                        />
                      </div>
                    ))}
                  </div>
                </div>

                <div>
                  <h4 className="mb-3 text-sm font-medium leading-snug">流式输出 / 上次评分结果</h4>
                  {streaming ? (
                    <p className="mb-2 text-xs leading-relaxed text-muted-foreground">处理中…</p>
                  ) : null}
                  <ScrollArea
                    className="min-h-[min(520px,55vh)] max-h-[min(900px,85vh)] rounded-lg border border-border bg-background"
                    viewportClassName="p-4"
                  >
                    <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-2 prose-headings:my-3">
                      <Markdown remarkPlugins={[remarkGfm]}>
                        {streamPreview || selectedExp.ranking_output || '（尚无）'}
                      </Markdown>
                    </div>
                  </ScrollArea>
                </div>

                <div>
                  <h4 className="mb-3 text-sm font-medium leading-snug">复盘笔记 / 案例摘要（可编辑后保存）</h4>
                  <textarea
                    className={`${controlClass} min-h-[140px] resize-y`}
                    value={reviewNote}
                    onChange={(e) => setReviewNote(e.target.value)}
                  />
                  <div className="mt-3">
                    <Input
                      placeholder="案例一句话摘要 case_summary"
                      value={caseSummary}
                      onChange={(e) => setCaseSummary(e.target.value)}
                    />
                  </div>
                  <Button className="mt-3" size="sm" variant="secondary" onClick={() => void saveCaseMeta()}>
                    保存复盘与摘要
                  </Button>
                </div>
              </div>
            ) : (
              <p className="mt-6 border-t border-border pt-6 text-sm leading-relaxed text-muted-foreground">
                请选择一条实验。
              </p>
            )}
          </SectionCard>
        </div>
      ) : null}

      {streamLines.length > 0 && tab === 'experiment' ? (
        <SectionCard title="原始 SSE 行（调试用，最多保留 200 行）">
          <pre className="max-h-40 overflow-auto rounded-md border border-border bg-muted/20 p-4 text-[10px] leading-relaxed text-muted-foreground">
            {streamLines.join('\n')}
          </pre>
        </SectionCard>
      ) : null}

      <ModalDialog
        isOpen={viewStrategy != null}
        title={viewStrategy ? `策略版本 · ${viewStrategy.version_label}` : ''}
        onClose={() => setViewStrategy(null)}
        maxWidthClass="max-w-4xl"
      >
        {viewStrategy ? (
          <div className="space-y-4 text-sm leading-relaxed">
            <p>
              <span className="text-muted-foreground">标题：</span>
              {viewStrategy.title}
            </p>
            <p className="font-mono text-xs text-muted-foreground">
              id={viewStrategy.id}
              {viewStrategy.parent_version_id != null ? ` · 父版本 id=${viewStrategy.parent_version_id}` : ''}
              {viewStrategy.created_at ? ` · ${viewStrategy.created_at}` : ''}
            </p>
            <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-2 prose-headings:my-3">
              <Markdown remarkPlugins={[remarkGfm]}>{viewStrategy.body_markdown || '（空）'}</Markdown>
            </div>
          </div>
        ) : null}
      </ModalDialog>

      <ModalDialog
        isOpen={viewExperimentModal != null || viewExperimentLoading}
        title={
          viewExperimentModal
            ? `实验 #${viewExperimentModal.id} · ${viewExperimentModal.trade_date ?? ''}`
            : '加载实验…'
        }
        onClose={() => {
          setViewExperimentLoading(false);
          setViewExperimentModal(null);
        }}
        maxWidthClass="max-w-5xl"
      >
        {viewExperimentLoading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : viewExperimentModal ? (
          <div className="space-y-5 text-sm leading-relaxed">
            <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted-foreground">
              <span>策略版本 id：{viewExperimentModal.strategy_version_id}</span>
              <span>状态：{viewExperimentModal.status}</span>
              <span>早盘指标条数：{viewExperimentModal.morning_metrics_count}</span>
            </div>
            <div>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">案例摘要</h3>
              <p className="rounded-md border border-border bg-muted/20 px-3 py-2 text-sm">
                {viewExperimentModal.case_summary || '—'}
              </p>
            </div>
            <div>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">粘贴原文</h3>
              <pre className="max-h-40 overflow-auto rounded-md border border-border bg-muted/30 p-3 text-xs leading-relaxed">
                {viewExperimentModal.pasted_raw}
              </pre>
            </div>
            {viewExperimentModal.param_snapshot ? (
              <div>
                <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">对话/参数快照</h3>
                <pre className="max-h-52 overflow-auto rounded-md border border-border bg-muted/30 p-3 text-xs leading-relaxed">
                  {JSON.stringify(viewExperimentModal.param_snapshot, null, 2)}
                </pre>
              </div>
            ) : null}
            <div>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">评分输出</h3>
              <ScrollArea className="max-h-[min(360px,50vh)] rounded-lg border border-border bg-background" viewportClassName="p-3">
                <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-2 prose-headings:my-3">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {viewExperimentModal.ranking_output || '（尚无）'}
                  </Markdown>
                </div>
              </ScrollArea>
            </div>
            <div>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">复盘笔记</h3>
              <ScrollArea className="max-h-[min(280px,40vh)] rounded-lg border border-border bg-muted/20" viewportClassName="p-3">
                <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-2 prose-headings:my-3">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {viewExperimentModal.review_note_markdown || '（尚无）'}
                  </Markdown>
                </div>
              </ScrollArea>
            </div>
            <div className="flex flex-wrap gap-2 border-t border-border pt-4">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => {
                  setTab('experiment');
                  setSelectedExpId(viewExperimentModal.id);
                  setViewExperimentModal(null);
                }}
              >
                在「实验与评分」中打开
              </Button>
              <Button type="button" variant="danger-subtle" size="sm" onClick={() => setDeleteConfirmExp(viewExperimentModal)}>
                删除此实验
              </Button>
            </div>
          </div>
        ) : null}
      </ModalDialog>

      <ConfirmDialog
        isOpen={deleteConfirmVersion != null}
        title="删除策略版本"
        message={
          deleteConfirmVersion
            ? `确定删除策略版本「${deleteConfirmVersion.version_label}」？若仍有实验引用该版本，删除将被服务器拒绝。`
            : ''
        }
        confirmText={deleteVersionBusy ? '删除中…' : '删除'}
        cancelText="取消"
        isDanger
        onConfirm={() => void confirmDeleteVersion()}
        onCancel={() => {
          if (!deleteVersionBusy) setDeleteConfirmVersion(null);
        }}
      />

      <ConfirmDialog
        isOpen={deleteConfirmExp != null}
        title="删除实验"
        message={
          deleteConfirmExp
            ? `确定删除实验 #${deleteConfirmExp.id}（${deleteConfirmExp.trade_date ?? ''}）？将同时移除早盘指标与该实验在问股中的会话消息，不可恢复。`
            : ''
        }
        confirmText={deleteBusy ? '删除中…' : '删除'}
        cancelText="取消"
        isDanger
        onConfirm={() => void confirmDeleteExperiment()}
        onCancel={() => {
          if (!deleteBusy) setDeleteConfirmExp(null);
        }}
      />
    </div>
  );
};

export default TailTacticsPage;
