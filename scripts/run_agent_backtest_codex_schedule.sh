#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${AGENT_BACKTEST_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUN_ID="${AGENT_BACKTEST_RUN_ID:-1}"
CODEX_BIN="${CODEX_BIN:-codex}"
TIMEZONE="${AGENT_BACKTEST_TIMEZONE:-Asia/Shanghai}"
LIVE_DATA="${AGENT_BACKTEST_LIVE_DATA:-true}"
RUN_WEEKENDS="${AGENT_BACKTEST_RUN_WEEKENDS:-false}"
DRY_RUN="false"
PHASE=""

export LANG="${LANG:-en_US.UTF-8}"

usage() {
  cat <<'EOF'
Usage: scripts/run_agent_backtest_codex_schedule.sh [--phase morning|midday|tail|close] [--dry-run]

Environment:
  AGENT_BACKTEST_RUN_ID       Agent backtest run id, default 1
  AGENT_BACKTEST_REPO_ROOT    Repository root
  AGENT_BACKTEST_LIVE_DATA    true/false, default true
  AGENT_BACKTEST_TIMEZONE     Default Asia/Shanghai
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
  case "$now_hm" in
    09:*|10:*|11:*)
      PHASE="morning"
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

case "$PHASE" in
  morning|midday|tail|close)
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
if [[ "$LIVE_DATA" == "true" && "$PHASE" != "close" ]]; then
  live_arg="--live-data"
fi

prompt="$(cat <<EOF
你是 Codex，在 DSA 仓库里执行 A 股三操盘手纸面交易实验的定时周期。

硬性上下文：
- repo: $REPO_ROOT
- run_id: $RUN_ID
- trade_date: $trade_date
- phase: $PHASE
- timezone: $TIMEZONE
- 今天只操作本回测系统，不修改策略代码，不 git commit/push。
- 短线、中线、长线三个操盘手必须严格上下文隔离；读取一个 profile 的 context 后，只为该 profile 决策，不引用其他 profile 的持仓、决策或理由。
- 符合 A 股规则：只交易 run.symbols，100 股整数手，T+1，买入现金约束，不能越过 data_cutoff_at 使用未来信息。

执行步骤：
1. 进入 repo，运行：
   python scripts/run_agent_backtest_cycle.py cycle --run-id $RUN_ID --phase $PHASE --trade-date $trade_date $live_arg
2. 如果 phase 不是 close，依次读取生成的 short_context.md、medium_context.md、long_context.md。
3. 为每个 profile 独立判断并调用 apply-decision 写回。允许 observe/hold/buy/sell；买卖必须给出 symbol、side、quantity、order_type、limit_price、submitted_at、effective_at。
4. 如果生成订单，只有在有可靠的成交/回放价格时才调用 fill-order；否则保持 pending，并在总结里说明原因。
5. 如果 phase 是 close，确认 daily-nav 已记录，并总结三账户现金、持仓、市值和当日事件。
6. 把本轮简短总结保存到：
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
