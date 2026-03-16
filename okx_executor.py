#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent.parent

BSC_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
DEFAULT_CACHE = WORKSPACE / "cache" / "binance_autotrader_okx_quote_probe.json"


@dataclass
class OkxQuoteResult:
    ok: bool
    status: str
    reason: str
    chain: str
    token_contract: str
    wallet_address: str
    from_token: str
    to_token: str
    amount_in: str
    route_count: int
    price_impact_pct: float
    gas_fee_native: float
    trade_fee_usd: float
    honeypot: bool
    tax_rate: float
    raw: dict[str, Any]


@dataclass
class OkxSwapResult:
    ok: bool
    status: str
    reason: str
    chain: str
    token_contract: str
    wallet_address: str
    from_token: str
    to_token: str
    amount_in: str
    route_count: int
    price_impact_pct: float
    gas_fee_native: float
    trade_fee_usd: float
    honeypot: bool
    tax_rate: float
    min_receive_amount: str
    tx: dict[str, Any]
    raw: dict[str, Any]


def _find_onchainos() -> str:
    return shutil.which("onchainos") or str(Path.home() / ".local" / "bin" / "onchainos")


def _quote_usdt_to_bnb_wei(quote_usdt: float, bnb_price_usdt: float) -> str:
    if bnb_price_usdt <= 0:
        raise ValueError("invalid_bnb_price")
    funds_bnb = Decimal(str(quote_usdt)) / Decimal(str(bnb_price_usdt))
    wei = int(funds_bnb * (10 ** 18))
    return str(max(0, wei))


def _run_onchainos(args: list[str], timeout_sec: int) -> dict[str, Any]:
    cli = _find_onchainos()
    cmd = [cli] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip(), "cmd": cmd}
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return {"ok": False, "error": f"invalid_json:{(proc.stdout or '')[:300]}", "cmd": cmd}
    return {"ok": True, "payload": payload, "cmd": cmd}


def _extract_quote_item(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or []
    return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})


def _extract_swap_item(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or []
    return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})


def normalize_okx_quote(raw: dict[str, Any], token_contract: str, wallet_address: str, from_token: str, amount_in: str, chain: str = "bsc", max_price_impact_pct: float = 8.0) -> OkxQuoteResult:
    if not raw.get("ok"):
        return OkxQuoteResult(
            ok=False,
            status="okx_quote_error",
            reason=str(raw.get("error") or "unknown_error"),
            chain=chain,
            token_contract=token_contract,
            wallet_address=wallet_address,
            from_token=from_token,
            to_token=token_contract,
            amount_in=amount_in,
            route_count=0,
            price_impact_pct=0.0,
            gas_fee_native=0.0,
            trade_fee_usd=0.0,
            honeypot=False,
            tax_rate=0.0,
            raw=raw,
        )

    item = _extract_quote_item(raw.get("payload") or {})
    route_list = item.get("dexRouterList") or []
    route_count = len(route_list)
    price_impact = float(item.get("priceImpactPercent") or 0.0)
    gas_fee_native = float(item.get("estimateGasFee") or 0.0)
    trade_fee_usd = float(item.get("tradeFee") or 0.0)
    to_token_meta = item.get("toToken") or {}
    honeypot = bool(to_token_meta.get("isHoneyPot"))
    tax_rate = float(to_token_meta.get("taxRate") or 0.0)

    if route_count <= 0:
        ok = False
        status = "okx_quote_no_route"
        reason = "no_route"
    elif honeypot:
        ok = False
        status = "okx_quote_honeypot"
        reason = "honeypot"
    elif price_impact > max_price_impact_pct:
        ok = False
        status = "okx_quote_high_impact"
        reason = f"price_impact_too_high:{price_impact:.2f}"
    else:
        ok = True
        status = "okx_quote_ok"
        reason = ""

    return OkxQuoteResult(
        ok=ok,
        status=status,
        reason=reason,
        chain=chain,
        token_contract=token_contract,
        wallet_address=wallet_address,
        from_token=from_token,
        to_token=token_contract,
        amount_in=amount_in,
        route_count=route_count,
        price_impact_pct=price_impact,
        gas_fee_native=gas_fee_native,
        trade_fee_usd=trade_fee_usd,
        honeypot=honeypot,
        tax_rate=tax_rate,
        raw=item,
    )


def normalize_okx_swap(raw: dict[str, Any], token_contract: str, wallet_address: str, from_token: str, amount_in: str, chain: str = "bsc", max_price_impact_pct: float = 8.0) -> OkxSwapResult:
    if not raw.get("ok"):
        return OkxSwapResult(
            ok=False,
            status="okx_swap_error",
            reason=str(raw.get("error") or "unknown_error"),
            chain=chain,
            token_contract=token_contract,
            wallet_address=wallet_address,
            from_token=from_token,
            to_token=token_contract,
            amount_in=amount_in,
            route_count=0,
            price_impact_pct=0.0,
            gas_fee_native=0.0,
            trade_fee_usd=0.0,
            honeypot=False,
            tax_rate=0.0,
            min_receive_amount="0",
            tx={},
            raw=raw,
        )

    item = _extract_swap_item(raw.get("payload") or {})
    router_result = item.get("routerResult") or {}
    tx = item.get("tx") or {}
    route_list = router_result.get("dexRouterList") or []
    route_count = len(route_list)
    price_impact = float(router_result.get("priceImpactPercent") or 0.0)
    gas_fee_native = float(router_result.get("estimateGasFee") or 0.0)
    trade_fee_usd = float(router_result.get("tradeFee") or 0.0)
    to_token_meta = router_result.get("toToken") or {}
    honeypot = bool(to_token_meta.get("isHoneyPot"))
    tax_rate = float(to_token_meta.get("taxRate") or 0.0)
    min_receive_amount = str(tx.get("minReceiveAmount") or router_result.get("toTokenAmount") or "0")

    if route_count <= 0:
        ok = False
        status = "okx_swap_no_route"
        reason = "no_route"
    elif honeypot:
        ok = False
        status = "okx_swap_honeypot"
        reason = "honeypot"
    elif price_impact > max_price_impact_pct:
        ok = False
        status = "okx_swap_high_impact"
        reason = f"price_impact_too_high:{price_impact:.2f}"
    elif not tx:
        ok = False
        status = "okx_swap_missing_tx"
        reason = "missing_tx"
    else:
        ok = True
        status = "okx_swap_ok"
        reason = ""

    return OkxSwapResult(
        ok=ok,
        status=status,
        reason=reason,
        chain=chain,
        token_contract=token_contract,
        wallet_address=wallet_address,
        from_token=from_token,
        to_token=token_contract,
        amount_in=amount_in,
        route_count=route_count,
        price_impact_pct=price_impact,
        gas_fee_native=gas_fee_native,
        trade_fee_usd=trade_fee_usd,
        honeypot=honeypot,
        tax_rate=tax_rate,
        min_receive_amount=min_receive_amount,
        tx=tx,
        raw=item,
    )


def okx_quote_token(token_contract: str, quote_usdt: float, bnb_price_usdt: float, wallet_address: str, chain: str = "bsc", max_price_impact_pct: float = 8.0, timeout_sec: int = 12) -> OkxQuoteResult:
    amount_in = _quote_usdt_to_bnb_wei(quote_usdt, bnb_price_usdt)
    raw = _run_onchainos([
        "swap", "quote",
        "--from", BSC_NATIVE,
        "--to", token_contract.lower(),
        "--amount", amount_in,
        "--chain", chain,
    ], timeout_sec=timeout_sec)
    return normalize_okx_quote(raw, token_contract=token_contract, wallet_address=wallet_address, from_token=BSC_NATIVE, amount_in=amount_in, chain=chain, max_price_impact_pct=max_price_impact_pct)


def okx_quote_sell_token(token_contract: str, amount_raw: int, wallet_address: str, chain: str = "bsc", max_price_impact_pct: float = 8.0, timeout_sec: int = 12) -> OkxQuoteResult:
    amount_in = str(max(0, int(amount_raw)))
    raw = _run_onchainos([
        "swap", "quote",
        "--from", token_contract.lower(),
        "--to", BSC_NATIVE,
        "--amount", amount_in,
        "--chain", chain,
    ], timeout_sec=timeout_sec)
    return normalize_okx_quote(raw, token_contract=token_contract, wallet_address=wallet_address, from_token=token_contract.lower(), amount_in=amount_in, chain=chain, max_price_impact_pct=max_price_impact_pct)


def okx_build_buy_swap(token_contract: str, quote_usdt: float, bnb_price_usdt: float, wallet_address: str, chain: str = "bsc", slippage_pct: float = 1.0, max_price_impact_pct: float = 8.0, timeout_sec: int = 15) -> OkxSwapResult:
    amount_in = _quote_usdt_to_bnb_wei(quote_usdt, bnb_price_usdt)
    raw = _run_onchainos([
        "swap", "swap",
        "--from", BSC_NATIVE,
        "--to", token_contract.lower(),
        "--amount", amount_in,
        "--chain", chain,
        "--wallet", wallet_address,
        "--slippage", str(slippage_pct),
    ], timeout_sec=timeout_sec)
    return normalize_okx_swap(raw, token_contract=token_contract, wallet_address=wallet_address, from_token=BSC_NATIVE, amount_in=amount_in, chain=chain, max_price_impact_pct=max_price_impact_pct)


def okx_build_sell_swap(token_contract: str, amount_raw: int, wallet_address: str, chain: str = "bsc", slippage_pct: float = 1.0, max_price_impact_pct: float = 8.0, timeout_sec: int = 15) -> OkxSwapResult:
    amount_in = str(max(0, int(amount_raw)))
    raw = _run_onchainos([
        "swap", "swap",
        "--from", token_contract.lower(),
        "--to", BSC_NATIVE,
        "--amount", amount_in,
        "--chain", chain,
        "--wallet", wallet_address,
        "--slippage", str(slippage_pct),
    ], timeout_sec=timeout_sec)
    return normalize_okx_swap(raw, token_contract=token_contract, wallet_address=wallet_address, from_token=token_contract.lower(), amount_in=amount_in, chain=chain, max_price_impact_pct=max_price_impact_pct)


def append_okx_probe_result(row: dict[str, Any], path: Path | None = None) -> None:
    target = path or DEFAULT_CACHE
    target.parent.mkdir(parents=True, exist_ok=True)
    data: list[dict[str, Any]] = []
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        except Exception:
            data = []
    data.append(row)
    data = data[-500:]
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def probe_row(result: OkxQuoteResult | OkxSwapResult, **extra: Any) -> dict[str, Any]:
    row = asdict(result)
    row.update(extra)
    row.setdefault("ts", int(time.time()))
    return row
