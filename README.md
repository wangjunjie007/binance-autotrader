# Binance AutoTrader

> 一个偏研究/实验性质的自动交易框架，支持 **Binance Spot** 与 **BSC 链上执行**，内置 dry-run / live 隔离、预算控制、日亏限制、止盈止损与多 worker 运行方式。

## Features

- **Multi-source signal pipeline**
  - 候选池、热点、地址跟踪、新闻/话题等多源信号汇总
- **Binance Spot execution**
  - 现货买卖、成交回填、数量规整、基础止盈止损
- **BSC on-chain execution**
  - 支持链上买卖、预估卖回检查、滑点/入场损失保护
- **Risk controls**
  - 单笔上限、每日预算、每日亏损上限、持仓管理、部分止盈
- **Safer runtime model**
  - dry-run / live 状态文件隔离
  - 链上交易等待回执后再改状态
  - pending nonce
- **Operational tooling**
  - 单进程 / 三 worker 启动脚本
  - 本地日志、状态文件、候选缓存

---

## Risk Warning

**This is not financial advice. This repository is for research / engineering / automation experiments.**

自动交易存在真实亏损风险，尤其是链上高波动、低流动性、带税代币场景。使用前请至少确认：

- 先用 `BINANCE_BOT_DRY_RUN=true` 完整验证
- 先小额实测，再逐步放大
- 配好单笔预算、每日预算、最大日亏
- 你自己理解滑点、税率、honeypot、流动性和链上执行风险

---

## Repository Structure

- `binance_autotrader.py` — 主策略与执行逻辑
- `okx_executor.py` — OKX / onchainos 相关封装
- `run_binance_autotrader.sh` — 单进程入口
- `run_binance_3workers.sh` — 三 worker 入口
- `run_binance_signals.sh` — 信号 worker
- `run_binance_trade.sh` — 交易 worker
- `run_binance_position_watch.sh` — 持仓观察 worker
- `requirements.txt` — Python 依赖
- `.env.example` — 配置模板
- `.gitignore` — 忽略敏感与运行时文件

---

## Requirements

### Python

建议 Python 3.11+

安装依赖：

```bash
pip install -r requirements.txt
```

当前最小依赖：

- `requests`
- `web3`

### External commands

如果启用链上 OKX swap 路径，还需要：

- `onchainos`

如果启用 Telegram / OpenClaw 消息提醒，还需要：

- `openclaw`

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/wangjunjie007/binance-autotrader.git
cd binance-autotrader
```

### 2. Prepare config

```bash
cp .env.example .env
```

至少先确认这些变量：

#### 通用
- `BINANCE_BOT_ENABLED`
- `BINANCE_BOT_DRY_RUN`
- `BINANCE_BOT_TESTNET`
- `BINANCE_BOT_MODE` (`all` / `signals` / `trade` / `positions`)

#### Binance Spot
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`

#### BSC / on-chain
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

### 3. Start in dry-run

```bash
./run_binance_autotrader.sh
```

或者三 worker：

```bash
./run_binance_3workers.sh
```

### 4. Inspect logs

```bash
tail -f ../../logs/binance-autotrader.log
```

---

## Runtime Notes

### Single-process mode

```bash
./run_binance_autotrader.sh
```

### Three-worker mode

```bash
./run_binance_3workers.sh
```

### Split workers

```bash
./run_binance_signals.sh
./run_binance_trade.sh
./run_binance_position_watch.sh
```

---

## Current Safety Improvements

当前版本已经补上的关键安全修复：

- dry-run / live 使用不同状态文件，避免污染
- spot 持仓补了模式标记
- spot 买入优先用真实成交回填持仓
- spot 卖出增加基础数量规整
- 链上交易等待回执后再记账/改状态
- 链上 nonce 改为 pending
- OKX 主买入路径补了基础入场风控

---

## Still Recommended Before Serious Capital Use

如果你要上更大资金，仍建议继续加强：

- 更完整的 Binance filters 处理
- 更细的 replaced / dropped / long-pending tx 状态机
- 更完整的回测 / 仿真 / replay 工具
- 更严格的 secrets 管理方案
- 更完善的监控与报警

---

## Open Source Hygiene

请不要提交以下文件：

- `.env`
- 日志文件
- 本地 state / cache
- 私钥、API Key、Cookie、Token

本仓库自带 `.gitignore`，但正式提交前仍建议人工复查一遍。

---

## License

暂未添加 LICENSE。

如果需要，我可以继续补：
- `MIT`
- `Apache-2.0`
- `GPL-3.0`
