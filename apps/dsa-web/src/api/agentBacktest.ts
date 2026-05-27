import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  AgentBacktestDailyNavResponse,
  AgentBacktestEventsResponse,
  AgentBacktestRunItem,
  AgentBacktestRunListResponse,
} from '../types/agentBacktest';

type RunListQuery = {
  status?: string;
  limit?: number;
};

type EventsQuery = {
  profileKey?: string;
  limit?: number;
};

function buildRunListParams(query: RunListQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.status) {
    params.status = query.status;
  }
  if (query.limit != null) {
    params.limit = query.limit;
  }
  return params;
}

function buildEventsParams(query: EventsQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.profileKey) {
    params.profile_key = query.profileKey;
  }
  if (query.limit != null) {
    params.limit = query.limit;
  }
  return params;
}

export const agentBacktestApi = {
  async listRuns(query: RunListQuery = {}): Promise<AgentBacktestRunListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/agent-backtest/runs', {
      params: buildRunListParams(query),
    });
    return toCamelCase<AgentBacktestRunListResponse>(response.data);
  },

  async getRun(runId: number): Promise<AgentBacktestRunItem> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/agent-backtest/runs/${runId}`);
    return toCamelCase<AgentBacktestRunItem>(response.data);
  },

  async listEvents(runId: number, query: EventsQuery = {}): Promise<AgentBacktestEventsResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/agent-backtest/runs/${runId}/events`, {
      params: buildEventsParams(query),
    });
    return toCamelCase<AgentBacktestEventsResponse>(response.data);
  },

  async recordDailyNav(runId: number, tradeDate: string): Promise<AgentBacktestDailyNavResponse> {
    const response = await apiClient.post<Record<string, unknown>>(`/api/v1/agent-backtest/runs/${runId}/daily-nav`, {
      trade_date: tradeDate,
    });
    return toCamelCase<AgentBacktestDailyNavResponse>(response.data);
  },
};
