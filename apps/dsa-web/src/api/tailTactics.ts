import apiClient from './index';

export interface TailStrategyVersion {
  id: number;
  version_label: string;
  title: string;
  body_markdown: string;
  parent_version_id: number | null;
  created_at: string | null;
}

export interface TailExperiment {
  id: number;
  trade_date: string | null;
  strategy_version_id: number;
  pasted_raw: string;
  symbols: string[];
  param_snapshot: Record<string, unknown> | null;
  ranking_session_id: string | null;
  ranking_output: string | null;
  review_note_markdown: string | null;
  case_summary: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  morning_metrics_count: number;
}

export interface TailMorningMetricRow {
  id: number;
  experiment_id: number;
  symbol: string;
  surge_pct_prev_close_930_1000: number | null;
  source: string;
  recorded_at: string | null;
}

export interface TailAgentCompose {
  session_id: string;
  message: string;
  context: Record<string, unknown>;
}

export const tailTacticsApi = {
  async listVersions(limit = 100): Promise<TailStrategyVersion[]> {
    const res = await apiClient.get<{ versions: TailStrategyVersion[] }>('/api/v1/tail-tactics/strategy-versions', {
      params: { limit },
    });
    return res.data.versions;
  },

  async createVersion(payload: {
    version_label: string;
    title: string;
    body_markdown: string;
    parent_version_id?: number | null;
  }): Promise<TailStrategyVersion> {
    const res = await apiClient.post<TailStrategyVersion>('/api/v1/tail-tactics/strategy-versions', payload);
    return res.data;
  },

  async getVersion(id: number): Promise<TailStrategyVersion> {
    const res = await apiClient.get<TailStrategyVersion>(`/api/v1/tail-tactics/strategy-versions/${id}`);
    return res.data;
  },

  async deleteVersion(id: number): Promise<void> {
    await apiClient.delete(`/api/v1/tail-tactics/strategy-versions/${id}`);
  },

  async diffVersions(a: number, b: number): Promise<{ unified_diff: string }> {
    const res = await apiClient.get<{ unified_diff: string }>(
      `/api/v1/tail-tactics/strategy-versions/${a}/diff/${b}`,
    );
    return res.data;
  },

  async listExperiments(params?: {
    limit?: number;
    strategy_version_id?: number;
    from_date?: string;
    to_date?: string;
  }): Promise<TailExperiment[]> {
    const res = await apiClient.get<{ experiments: TailExperiment[] }>('/api/v1/tail-tactics/experiments', {
      params,
    });
    return res.data.experiments;
  },

  async createExperiment(payload: {
    trade_date: string;
    strategy_version_id: number;
    pasted_raw: string;
    symbols: string[];
    param_snapshot?: Record<string, unknown> | null;
    status?: string;
  }): Promise<TailExperiment> {
    const res = await apiClient.post<TailExperiment>('/api/v1/tail-tactics/experiments', payload);
    return res.data;
  },

  async getExperiment(id: number): Promise<TailExperiment> {
    const res = await apiClient.get<TailExperiment>(`/api/v1/tail-tactics/experiments/${id}`);
    return res.data;
  },

  async patchExperiment(id: number, patch: Partial<{
    ranking_session_id: string | null;
    ranking_output: string | null;
    review_note_markdown: string | null;
    case_summary: string | null;
    param_snapshot: Record<string, unknown> | null;
    status: string | null;
  }>): Promise<TailExperiment> {
    const res = await apiClient.patch<TailExperiment>(`/api/v1/tail-tactics/experiments/${id}`, patch);
    return res.data;
  },

  async deleteExperiment(id: number): Promise<void> {
    await apiClient.delete(`/api/v1/tail-tactics/experiments/${id}`);
  },

  async compose(experimentId: number, kind: 'rank' | 'review'): Promise<TailAgentCompose> {
    const res = await apiClient.get<TailAgentCompose>(`/api/v1/tail-tactics/experiments/${experimentId}/compose`, {
      params: { kind },
    });
    return res.data;
  },

  async putMorningMetrics(
    experimentId: number,
    items: { symbol: string; surge_pct_prev_close_930_1000?: number | null; source?: string }[],
  ): Promise<void> {
    await apiClient.put(`/api/v1/tail-tactics/experiments/${experimentId}/morning-metrics`, { items });
  },

  async getMorningMetrics(experimentId: number): Promise<TailMorningMetricRow[]> {
    const res = await apiClient.get<{ items: TailMorningMetricRow[] }>(
      `/api/v1/tail-tactics/experiments/${experimentId}/morning-metrics`,
    );
    return res.data.items;
  },

  async autoFetchMorningMetrics(
    experimentId: number,
    payload?: { morning_trade_date?: string | null },
  ): Promise<{ morning_trade_date: string; notes: string[]; items: TailMorningMetricRow[] }> {
    const res = await apiClient.post<{
      morning_trade_date: string;
      notes: string[];
      items: TailMorningMetricRow[];
    }>(`/api/v1/tail-tactics/experiments/${experimentId}/morning-metrics/auto-fetch`, payload ?? {});
    return res.data;
  },
};
