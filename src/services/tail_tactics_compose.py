# -*- coding: utf-8 -*-
"""Build Agent chat payloads for tail tactics scoring / morning review."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _session_id_for_experiment(experiment_id: int) -> str:
    return f"tail_exp_{experiment_id}"


def build_ranking_compose(
    *,
    experiment_id: int,
    experiment: Dict[str, Any],
    strategy: Dict[str, Any],
    candidate_facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    symbols: List[str] = list(experiment.get("symbols") or [])
    primary = symbols[0] if symbols else ""
    bundle: Dict[str, Any] = {
        "mode": "score",
        "experiment_id": experiment_id,
        "trade_date": experiment.get("trade_date"),
        "strategy_title": strategy.get("title"),
        "strategy_version_label": strategy.get("version_label"),
        "strategy_body_markdown": strategy.get("body_markdown"),
        "candidates": [{"code": c} for c in symbols],
        "primary_symbol": primary,
        "pasted_raw": experiment.get("pasted_raw"),
        "param_snapshot": experiment.get("param_snapshot"),
        "candidate_facts": candidate_facts,
    }
    message = (
        "请根据系统补充的「尾盘候选逐票评分任务」和 `candidate_facts` 事实包进行评分。"
        "注意把策略拆成两层理解：第一层是用户在同花顺/动态选股中使用的「候选池生成规则」，"
        "只用于解释这些股票为什么进入候选池；第二层才是 Agent 的「评估/预测规则」，"
        "用于逐票评分、风险闸门、动作级别和次日开盘预测。不要把 `watch/candidate/priority`、"
        "`next_open_forecast` 或风险扣分反向混入第一层同花顺筛选条件。"
        "DSA 提供的是证据层，不是最终结论；你需要独立判断数据缺口、信号冲突、"
        "风险闸门和每只股票是否通过阈值。仅在事实包缺失或明显过期时再使用工具补充行情、"
        "K 线、技术与舆情等数据，且补充数据必须受 `selection_cutoff` 约束；"
        "不得使用 T 日 14:40 之后才发生的数据给 14:40 的候选打分。"
        "`selection_cutoff_evidence.fields` 中如存在分钟量能字段（如 `last_volume`、`window_volume`、"
        "`last_5m_volume_share`、`last_5m_vs_previous_30m_volume_ratio`），必须纳入 `price_volume` "
        "和 `liquidity` 判断：放量主动上攻或缩量稳住可加分，放量滞涨、放量回落或尾盘量价背离应扣分。"
        "即使候选池只有一只，也不能为了输出而推荐；低质量候选必须给 `reject` 或 `watch`。"
        "最终回答请先输出面向用户阅读的中文报告，再附机器可解析 JSON。"
        "中文报告使用轻量结构：`结论卡`、`关键证据`、`主要风险`、`次日开盘预判`、"
        "`明日观察/失效条件`；如需提出策略修改，必须明确标注是修改第一层「尾盘选股策略」"
        "还是第二层「Agent 评估/预测策略」。候选较多时用表格汇总，避免先展示大段 JSON。"
        "回答末尾必须包含一段 **纯 JSON**（markdown 代码块或直接 JSON 均可），"
        "且 JSON 根对象须包含键 `tail_score_result`，其值为数组；数组元素按用户候选顺序输出，"
        "字段至少包含：`code`、`score`(0-100 数字)、`action_level`(`reject`/`watch`/`candidate`/`priority`)、"
        "`confidence`(`low`/`medium`/`high`)、`factor_scores`(对象，含 market/sector/price_volume/liquidity/"
        "catalyst/risk_gate)、`risk_flags`(字符串数组)、`data_quality_flags`(字符串数组)、"
        "`next_day_watch`(字符串数组)、`next_open_forecast`(对象)、"
        "`invalidation_conditions`(字符串数组)、`one_line_reason`(字符串)。"
        "`next_open_forecast` 用于给出 T+1 开盘方向和大致区间，至少包含 "
        "`direction`(`gap_up`/`flat`/`gap_down`/`uncertain`)、"
        "`expected_open_pct_range`(二元数字数组)、`expected_open_price_range`(二元数字数组或 null)、"
        "`base_price`(数字或 null)、`confidence`、`reasoning`、`key_levels`。"
        "若报告生成时已经有 T 日收盘后数据，这些盘后数据只能用于 `next_open_forecast`，"
        "且必须在 `data_quality_flags` 或 `reasoning` 中说明；不得反向影响 14:40 的 `score`、"
        "`action_level` 或 `passed_candidates`。"
        "可以另外给出 `passed_candidates`，但只有 `action_level` 为 `candidate` 或 `priority` 的股票才能进入；"
        "如果整体环境或单票质量不好，`passed_candidates` 必须为空。"
    )
    context: Dict[str, Any] = {
        "stock_code": primary,
        "tail_ranking_bundle": bundle,
    }
    return {
        "session_id": _session_id_for_experiment(experiment_id),
        "message": message,
        "context": context,
    }


def build_review_compose(
    *,
    experiment_id: int,
    experiment: Dict[str, Any],
    strategy: Dict[str, Any],
    morning_metrics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    symbols: List[str] = list(experiment.get("symbols") or [])
    primary = symbols[0] if symbols else ""
    bundle: Dict[str, Any] = {
        "mode": "review",
        "experiment_id": experiment_id,
        "trade_date": experiment.get("trade_date"),
        "strategy_title": strategy.get("title"),
        "strategy_version_label": strategy.get("version_label"),
        "strategy_body_markdown": strategy.get("body_markdown"),
        "ranking_output": experiment.get("ranking_output"),
        "param_snapshot": experiment.get("param_snapshot"),
        "morning_metrics": morning_metrics,
        "symbols": symbols,
        "primary_symbol": primary,
    }
    message = (
        "请根据系统补充的「次日早盘复盘任务」：结合前一日的评分结论、"
        "用户策略版本与已准备的 9:30–10:00 冲高数据（相对昨收，单位 %），"
        "输出详细复盘：哪些预测命中/未命中、可能原因、对策略的改进建议。"
        "复盘建议必须分清：是第一层同花顺候选池筛选条件需要调整，"
        "还是第二层 Agent 评估/预测权重需要调整；不要把二者混写。"
        "结尾请给出可写入案例库的 `case_summary`（一句话）建议，"
        "并放在一段 JSON 中，根对象键名固定为 `tail_review_suggestions`，"
        "仅包含 `case_summary` 字段。"
    )
    context: Dict[str, Any] = {
        "stock_code": primary,
        "tail_review_bundle": bundle,
    }
    return {
        "session_id": _session_id_for_experiment(experiment_id),
        "message": message,
        "context": context,
    }


def format_tail_ranking_context_message(bundle: Dict[str, Any]) -> str:
    """Human-readable block injected before the user turn (executor)."""
    lines = [
        "[系统补充 · 尾盘候选逐票评分任务]",
        f"实验 ID: {bundle.get('experiment_id')}",
        f"交易日 T: {bundle.get('trade_date')}",
        f"策略版本: {bundle.get('strategy_version_label')} — {bundle.get('strategy_title')}",
        "",
        "## 用户策略正文（当前版本）",
        str(bundle.get("strategy_body_markdown") or "").strip(),
        "",
        "## 当日粘贴池原文",
        str(bundle.get("pasted_raw") or "").strip(),
        "",
        "## 对话/参数快照",
        json.dumps(bundle.get("param_snapshot") or {}, ensure_ascii=False, indent=2),
        "",
        "## 候选代码（按用户顺序）",
        json.dumps([c.get("code") for c in bundle.get("candidates") or []], ensure_ascii=False),
    ]
    candidate_facts = bundle.get("candidate_facts")
    if candidate_facts:
        lines.extend(
            [
                "",
                "## DSA 候选池事实包（证据层，不是结论）",
                json.dumps(candidate_facts, ensure_ascii=False, indent=2),
            ]
        )
    lines.extend(
        [
            "",
            "## 输出要求",
            "- 必须区分两层：第一层「尾盘选股策略」负责候选池生成，第二层「Agent 评估/预测策略」负责评分、风险闸门、动作级别和开盘预测。",
            "- 不要把第二层的 `watch/candidate/priority`、`next_open_forecast` 或风险扣分反向混入第一层同花顺筛选条件；策略修改建议要注明影响哪一层。",
            "- 优先使用 `candidate_facts` 中的结构化事实；事实缺失或过期时再使用工具补充，禁止编造行情数字。",
            "- `selection_cutoff_evidence.fields` 里的分钟价格和分钟量能要一起看；分钟量能字段应进入 `price_volume` / `liquidity` 判断，避免只看价格位置。",
            "- 打分只能基于 `selection_cutoff` 及之前可观察数据；T 日收盘价、全天最高价、收盘成交额等盘后字段只能作为复盘参考，不得用于评分。",
            "- 明确说明事实包的数据缺口、时间阶段和你自己的判断修正，不要把 DSA 缓存直接当最终评分。",
            "- 先输出适合用户阅读的中文报告，再把纯 JSON 放在最后作为机器解析附录；不要让大段 JSON 成为报告主体。",
            "- 先执行风险闸门，再逐票给分与动作级别；必须包含可解析的 `tail_score_result` JSON（见用户消息说明）。",
            "- 每票必须给出 `next_open_forecast`，用来描述 T+1 开盘方向、预估涨跌幅区间、价格区间和关键价位；这是预测补充，不得反向改变 14:40 评分。",
            "- 不需要强行推荐。候选池只有一只且质量差时，也必须给 `reject` 或 `watch`，并让 `passed_candidates` 为空。",
            "- `action_level=priority` 只表示研究优先级最高，不等于投资建议或自动交易指令。",
        ]
    )
    return "\n".join(lines)


def format_tail_review_context_message(bundle: Dict[str, Any]) -> str:
    lines = [
        "[系统补充 · 次日早盘复盘任务]",
        f"实验 ID: {bundle.get('experiment_id')}",
        f"交易日 T: {bundle.get('trade_date')}",
        f"策略版本: {bundle.get('strategy_version_label')} — {bundle.get('strategy_title')}",
        "",
        "## 策略正文",
        str(bundle.get("strategy_body_markdown") or "").strip(),
        "",
        "## 前序评分输出（模型或用户保存）",
        str(bundle.get("ranking_output") or "(无)").strip(),
        "",
        "## 对话/参数快照",
        json.dumps(bundle.get("param_snapshot") or {}, ensure_ascii=False, indent=2),
        "",
        "## 次日早盘冲高（相对昨收 %；自动拉取为 9:30–10:01 五分钟 K 或日线回退）",
        json.dumps(bundle.get("morning_metrics") or [], ensure_ascii=False, indent=2),
    ]
    lines.extend(
        [
            "",
            "## 输出要求",
            "- 对比预测与结果，说明评分口径。",
            "- 复盘建议必须分清：是第一层同花顺候选池筛选条件需要调整，还是第二层 Agent 评估/预测权重需要调整。",
            "- 结尾 JSON 块包含 `tail_review_suggestions`（仅含 case_summary）。",
        ]
    )
    return "\n".join(lines)
