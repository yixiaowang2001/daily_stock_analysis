# IBKR Flex 持仓同步

本功能通过 **Interactive Brokers Flex Statement Web Service** 拉取报表（CSV），解析 **Open Positions** 后写入按组合账户维度的缓存。存在缓存时，该账户的持仓快照 **不再由本地成交重算**，避免与 IB 报表不一致。

## 与 TWS / IB Gateway 的区别

- **不是** 实时行情或下单通道；**无** `ib_insync` / 本地网关。
- 适合对账、分析与展示；报表生成与拉取有延迟，属 **快照** 语义。

## IB 侧配置

1. 登录 [Client Portal](https://www.interactivebrokers.com/)，创建 **Flex Query**。
2. 查询中需包含 **Open Positions**，输出格式为 **CSV**。
3. 生成 **Flex Web Service Token**，记下 **Query ID**。

若列名与默认解析器不一致（不同模板字段名不同），需调整 `src/services/ibkr_flex_service.py` 中的列名映射。

## 服务端环境变量

| 变量 | 说明 |
|------|------|
| `IBKR_FLEX_TOKEN` | Flex Web Service Token |
| `IBKR_FLEX_QUERY_ID` | Flex Query ID |
| `HTTPS_PROXY` / `HTTP_PROXY` | 可选，受限网络访问 IB 时使用 |

可在 **Web 系统设置 → 基础设置 → 券商账号连接** 中填写 Token 与 Query ID（与 `.env` 等价，保存后写入已持久化配置）。模板亦见仓库根目录 `.env.example`。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/portfolio/ibkr-flex/refresh` | JSON body：`{"account_id": <int>}`，拉取 Flex、更新缓存并刷新该账户快照 |
| `DELETE` | `/api/v1/portfolio/ibkr-flex/cache?account_id=<int>` | 清除该账户 Flex 缓存，之后持仓恢复为本地成交重算 |

未配置 Token/Query ID 时，`refresh` 返回 **503**，`detail.error` 为 `ibkr_flex_not_configured`。

## 组合语义（重要）

- **持仓数量与市值**：以 IB Flex 解析结果为准（换算到账户 **基准货币**）。
- **现金**：仍来自该账户的 **资金流水**（`cash ledger`），不与 Flex 现金段自动合并。
- **已实现盈亏 / 手续费 / 税**：当前 **不与** 本地成交合并，Flex 模式下这些合计项为 0；若需完整损益，请后续单独导入成交或扩展设计。

## Web 界面

组合分析页提供「从 IBKR Flex 拉取持仓」与「清除 IBKR 缓存」，需先选择具体账户。

## 多账户报表

若一份 Flex CSV 包含多个 IB 子账户，当前版本将 **整表持仓** 写入你选择的 **一个** DSA 组合账户。按需为不同子账户建立多个 Flex Query 或多个 DSA 账户分别同步。
