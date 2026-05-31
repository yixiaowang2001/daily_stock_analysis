#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${AGENT_BACKTEST_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUN_ID="${AGENT_BACKTEST_RUN_ID:-1}"
CODEX_BIN="${CODEX_BIN:-codex}"
MARKET="${AGENT_BACKTEST_MARKET:-cn}"
DEFAULT_TIMEZONE="Asia/Shanghai"
if [[ "$MARKET" == "us" ]]; then
  DEFAULT_TIMEZONE="America/New_York"
fi
TIMEZONE="${AGENT_BACKTEST_TIMEZONE:-$DEFAULT_TIMEZONE}"
LIVE_DATA="${AGENT_BACKTEST_LIVE_DATA:-true}"
RUN_WEEKENDS="${AGENT_BACKTEST_RUN_WEEKENDS:-false}"
DRY_RUN="false"
PHASE=""

export LANG="${LANG:-en_US.UTF-8}"

usage() {
  cat <<'EOF'
Usage: scripts/run_agent_backtest_codex_schedule.sh [--phase morning|late_morning|pre_noon|midday|tail|close] [--dry-run]

Environment:
  AGENT_BACKTEST_RUN_ID       Agent backtest run id, default 1
  AGENT_BACKTEST_MARKET       cn/us, default cn
  AGENT_BACKTEST_REPO_ROOT    Repository root
  AGENT_BACKTEST_LIVE_DATA    true/false, default true
  AGENT_BACKTEST_TIMEZONE     Default Asia/Shanghai for cn, America/New_York for us
  CODEX_BIN                   Codex CLI path
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase)
      PHASE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$REPO_ROOT" ]]; then
  echo "Repository not found: $REPO_ROOT" >&2
  exit 1
fi

now_hm="$(TZ="$TIMEZONE" date '+%H:%M')"
trade_date="$(TZ="$TIMEZONE" date '+%F')"
weekday="$(TZ="$TIMEZONE" date '+%u')"

if [[ "$RUN_WEEKENDS" != "true" && ( "$weekday" == "6" || "$weekday" == "7" ) ]]; then
  echo "Skip non-weekday run at $trade_date $now_hm $TIMEZONE"
  exit 0
fi

if [[ -z "$PHASE" ]]; then
  if [[ "$MARKET" == "us" ]]; then
    case "$now_hm" in
      09:*)
        PHASE="morning"
        ;;
      10:*)
        PHASE="late_morning"
        ;;
      11:*|12:*)
        PHASE="pre_noon"
        ;;
      13:*|14:*)
        PHASE="midday"
        ;;
      15:*)
        PHASE="tail"
        ;;
      16:*|17:*|18:*|19:*)
        PHASE="close"
        ;;
      20:*|21:*|22:*|23:*|00:*|01:*|02:*|03:*)
        PHASE="tail"
        ;;
      *)
        echo "Cannot infer US phase for $now_hm $TIMEZONE; pass --phase explicitly." >&2
        exit 1
        ;;
    esac
  else
    case "$now_hm" in
      09:*)
        PHASE="morning"
        ;;
      10:*)
        PHASE="late_morning"
        ;;
      11:*)
        PHASE="pre_noon"
        ;;
      13:*|14:0*|14:1*|14:2*)
        PHASE="midday"
        ;;
      14:3*|14:4*|14:5*|15:00)
        PHASE="tail"
        ;;
      15:*|16:*|17:*|18:*)
        PHASE="close"
        ;;
      *)
        echo "Cannot infer phase for $now_hm $TIMEZONE; pass --phase explicitly." >&2
        exit 1
        ;;
    esac
  fi
fi

case "$PHASE" in
  morning|late_morning|pre_noon|midday|tail|close)
    ;;
  *)
    echo "Unsupported phase: $PHASE" >&2
    exit 2
    ;;
esac

log_dir="$REPO_ROOT/.claude/reviews/agent_backtest/run_${RUN_ID}/logs"
mkdir -p "$log_dir"
log_file="$log_dir/${trade_date}_${PHASE}.log"
lock_dir="$REPO_ROOT/.claude/reviews/agent_backtest/run_${RUN_ID}/.schedule-lock"

if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "Another agent backtest scheduled run is active; skip $trade_date $PHASE." | tee -a "$log_file"
  exit 0
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

live_arg=""
if [[ "$LIVE_DATA" == "true" ]]; then
  live_arg="--live-data"
fi

if [[ "$MARKET" == "us" ]]; then
  market_intro="美股三操盘手现金账户纸面交易实验"
  market_rules="符合美股现金账户规则：买入/加仓只限 context 中 buy_allowed_symbols；exit_only_symbols 只能继续研究、持有、减仓或卖出；整股交易；买入只能使用 settled cash；当日或未结算卖出资金 T+1 美股工作日才可用；滚动 5 个美股工作日最多 1 次日内回转；盘前/盘后/隔夜交易必须用限价单并说明流动性、价差和 session 风险。"
else
  market_intro="A 股三操盘手纸面交易实验"
  market_rules="符合 A 股规则：买入/加仓只限 context 中 buy_allowed_symbols；exit_only_symbols 只能继续研究、持有、减仓或卖出；100 股整数手，T+1，买入现金约束，不能越过 data_cutoff_at 使用未来信息。"
fi

prompt="$(cat <<EOF
你是 Codex，在 DSA 仓库里执行${market_intro}的定时周期。

硬性上下文：
- repo: $REPO_ROOT
- run_id: $RUN_ID
- market: $MARKET
- trade_date: $trade_date
- phase: $PHASE
- timezone: $TIMEZONE
- 今天只操作本回测系统，不修改策略代码，不 git commit/push。
- 所有 active profile 必须严格上下文隔离；读取一个 profile 的 context 后，只为该 profile 决策，不引用其他 profile 的持仓、决策或理由。
- $market_rules

执行步骤：
1. 进入 repo，运行：
   python scripts/run_agent_backtest_cycle.py cycle --run-id $RUN_ID --phase $PHASE --trade-date $trade_date $live_arg
2. 如果 phase 不是 close，依次读取 cycle 输出 generated 列表中的每个 context_markdown。
3. 每个 profile 决策前先检查 evidence.symbol_facts[*].information_context 和 codex_research_fallback；若关注/持仓标的 status=recommended，使用 Codex 可用的联网/搜索/browser 工具补搜公开信息，优先交易所/公司公告和有时间戳的可靠财经媒体，只使用 data_cutoff_at 之前的信息，并在 rationale、risk_notes 和总结里写明标题/日期/URL；如果搜索工具不可用，明确记录数据缺口。
4. 为每个 profile 独立判断并调用 apply-decision 写回。允许 observe/hold/buy/sell；不要求每轮必须交易，是否交易由该 profile 的策略自行决定；买卖必须给出 symbol、side、quantity、order_type、limit_price、submitted_at、effective_at。
5. 如果生成订单，只有在有可靠的成交/回放价格时才调用 fill-order；否则保持 pending，并在总结里说明原因。
6. 如果 phase 是 close，确认 daily-nav 已记录；逐个读取生成的 close context，为每个 profile 写一份收盘自评 markdown。若某个 profile 发现可复用的策略教训，再用 evolve-policy 写入新的前向策略版本；不要为了单日噪音强行改策略。
7. 把本轮简短总结保存到：
   .claude/reviews/agent_backtest/run_${RUN_ID}/${trade_date}/${PHASE}/codex_summary.md

最终回复用中文，包含：本轮 phase、观察/决策/订单/成交/净值、未完成项和数据质量问题。
EOF
)"

{
  echo "==== $(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z') run_id=$RUN_ID phase=$PHASE ===="
  echo "repo=$REPO_ROOT"
  echo "live_data=$LIVE_DATA"
} >> "$log_file"

if [[ "$DRY_RUN" == "true" ]]; then
  printf '%s\n' "$prompt"
  exit 0
fi

if [[ "$CODEX_BIN" == */* ]]; then
  CODEX_CMD="$CODEX_BIN"
else
  CODEX_CMD="$(command -v "$CODEX_BIN" || true)"
fi

if [[ -z "${CODEX_CMD:-}" || ! -x "$CODEX_CMD" ]]; then
  echo "Codex CLI not executable or not found: $CODEX_BIN" | tee -a "$log_file" >&2
  exit 1
fi

"$CODEX_CMD" exec \
  --cd "$REPO_ROOT" \
  --sandbox danger-full-access \
  --ask-for-approval never \
  "$prompt" >> "$log_file" 2>&1
