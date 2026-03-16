# Binance AutoTrader

一个偏研究/实验性质的自动交易脚本框架，支持：
- Binance Spot 信号筛选与下单
- BSC 链上代币买卖（含风控）
- 多 worker 运行模式（signals / trade / positions）
- dry-run 与 live 状态隔离
- 本地日志、状态记录、候选缓存

## 重要风险说明

**这不是稳赚脚本。**
任何自动交易都可能亏损，尤其是链上高波动/低流动性资产。

使用前请至少确认：
- 先用 `BINANCE_BOT_DRY_RUN=true` 跑通
- 先小额验证，再逐步放大
- 明确配置单笔上限、每日上限、最大亏损上限
- 自己理解 Binance / BSC / 路由 / 滑点 / 税率 / honeypot 风险

## 依赖

### Python
建议 Python 3.11+

安装依赖：

```bash
pip install -r requirements.txt
```

当前最小依赖：
- requests
- web3

### 外部命令
如果启用链上 OKX swap 路径，还需要：
- `onchainos`

如果启用 Telegram/OpenClaw 消息提醒，还需要：
- `openclaw`

## 配置

复制配置模板：

```bash
cp .env.example .env
```

### 核心变量

#### 通用
- `BINANCE_BOT_ENABLED`
- `BINANCE_BOT_DRY_RUN`
- `BINANCE_BOT_TESTNET`
- `BINANCE_BOT_MODE`：`all` / `signals` / `trade` / `positions`

#### Binance Spot
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`

#### BSC 链上执行
- `BSC_RPC_HTTPS`
- `BINANCE_BOT_WALLET_ADDRESS`
- `BINANCE_BOT_WALLET_PRIVATE_KEY`
- 可选：`BINANCE_BOT_WALLET_JSON`

#### 风控
- `BINANCE_BOT_MAX_USDT_PER_TRADE`
- `BINANCE_BOT_MAX_DAILY_USDT`
- `BINANCE_BOT_MAX_DAILY_LOSS_USDT`
- `BINANCE_BOT_MIN_SCORE`
- `BINANCE_BOT_TAKE_PROFIT_PCT`
- `BINANCE_BOT_STOP_LOSS_PCT`
- `BINANCE_BOT_ONCHAIN_MIN_SELLBACK_RATIO`
- `BINANCE_BOT_ONCHAIN_SLIPPAGE_BPS`

## 运行

### 单进程

```bash
./run_binance_autotrader.sh
```

### 三 worker

```bash
./run_binance_3workers.sh
```

### 分别运行

```bash
./run_binance_signals.sh
./run_binance_trade.sh
./run_binance_position_watch.sh
```

## 文件说明

- `binance_autotrader.py`：主逻辑
- `okx_executor.py`：OKX / onchainos 相关封装
- `requirements.txt`：Python 依赖
- `.env.example`：配置模板
- `.gitignore`：忽略敏感/运行时文件

## 当前实现特点

- dry-run / live 使用不同状态文件，避免互相污染
- 链上交易在记账/改持仓前会等待交易回执
- 链上 nonce 使用 pending 模式
- OKX 主买入路径补了基础入场风控
- spot 买入会优先用真实成交回填持仓
- spot 卖出会做基础数量规整

## 仍建议你自行补充/验证

在真实资金环境里，建议继续验证：
- 不同交易对的 Binance filters 完整覆盖
- 链上失败交易、长时间 pending、replaced tx 的处理
- 更完整的 README / 回测说明 / 示例日志
- 更细的 secrets 管理方案

## 开源注意事项

不要提交以下文件：
- `.env`
- 运行日志
- 本地 state / cache
- 私钥、API Key、Cookie、Token

本目录已经自带 `.gitignore`，但你在正式上传前仍应再次人工检查。
