# 币安自动交易脚本演示 Runbook

## 1) 你可以这样开场（30秒）
大家好，我做了一个“多插件融合”的币安自动交易脚本，不是只看一个信号就下单。
它会同时读取 Binance Skills Hub 里的多个能力：Smart Money、Meme Rush、Topic Rush、Token Info、Token Audit、Address Info，再叠加 Binance Spot 的真实交易接口做风控执行。
重点是：策略可解释、限额可控、默认先模拟，验证通过再切实盘。

---

## 2) 你要重点介绍的功能（建议按这个顺序）
1. **多源信号融合打分**
   - `trading-signal`：智能钱买卖信号
   - `crypto-market-rank`：趋势榜/Alpha榜
   - `meme-rush + topic-rush`：Meme 热度和主题资金流
2. **交易前质量门控**
   - `query-token-info`：流动性、持币人数、波动等动态指标
   - `query-token-audit`：合约风险等级，自动扣分或拦截
3. **执行与风控**
   - `spot`（Binance Spot API）：下单、查价
   - 单笔限额、日限额、止盈止损
4. **可观测与可复盘**
   - 全程日志
   - 本地状态记录（预算与持仓）
   - 可选地址持仓监控（`query-address-info`）

---

## 3) 演示流程（3-5分钟）

### 第一步：展示配置
```bash
cd scripts/binance-autotrader
cat .env
```
讲解点：
- `BINANCE_BOT_DRY_RUN=true` 先做安全演示
- `BINANCE_BOT_MAX_USDT_PER_TRADE=20`
- `BINANCE_BOT_MAX_DAILY_USDT=20`

### 第二步：启动脚本
```bash
nohup ./run_binance_autotrader.sh >/dev/null 2>&1 &
pgrep -fl binance_autotrader.py
```
讲解点：
- 常驻运行、带锁防重入
- 支持长期轮询

### 第三步：实时看日志
```bash
tail -f ../../logs/binance-autotrader.log
```
讲解点：
- 候选数量
- 打分与跳过原因
- 达到阈值会触发买入（模拟盘会显示 dryRun 订单）

### 第四步：展示限额/风控
当日志出现交易行为时强调：
- 单笔不会超过 20 USDT
- 当天累计不会超过 20 USDT
- 持仓进入 TP/SL 会自动处理

### 第五步：收尾
```bash
pkill -f binance_autotrader.py
```
讲解点：
- 可随时停机
- 策略参数可快速调整

---

## 4) 结束话术（20秒）
这个脚本不是“无脑冲单”，而是“多插件信号融合 + 风险优先”的执行框架。
我的目标是把可复用的交易基础设施搭好：先验证策略稳定，再小额实盘，最后再逐步扩大策略覆盖面。
