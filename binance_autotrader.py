#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import requests
from web3 import Web3

from okx_executor import (
    append_okx_probe_result,
    okx_build_buy_swap,
    okx_build_sell_swap,
    okx_quote_sell_token,
    okx_quote_token,
    probe_row,
)

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent.parent

CACHE_DIR = WORKSPACE / "cache"
LOG_DIR = WORKSPACE / "logs"
STATE_FILE = CACHE_DIR / "binance_autotrader_state.json"
CANDIDATE_FILE = CACHE_DIR / "binance_autotrader_candidates.json"


def current_state_file(dry_run: bool | None = None) -> Path:
    if dry_run is None:
        raw = (os.getenv("BINANCE_BOT_DRY_RUN") or "true").strip().lower()
        dry_run = raw in {"1", "true", "yes", "on"}
    suffix = "dry" if dry_run else "live"
    return CACHE_DIR / f"binance_autotrader_state.{suffix}.json"
OKX_QUOTE_PROBE_FILE = CACHE_DIR / "binance_autotrader_okx_quote_probe.json"
EVOLVE_REVIEW_FILE = LOG_DIR / "binance-autotrader-evolve.jsonl"
DEFAULT_LOG_FILE = LOG_DIR / "binance-autotrader.log"
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")


def load_okx_wallet_address() -> str:
    for key in ["OKX_WALLET_ADDRESS", "BINANCE_BOT_WALLET_ADDRESS", "BASE_WALLET_ADDRESS"]:
        env_addr = (os.getenv(key) or "").strip()
        if env_addr:
            return env_addr
    return ""


def load_okx_bsc_rpc() -> str:
    for key in ["BSC_RPC_HTTPS", "BINANCE_BOT_BSC_RPC_HTTPS", "OKX_BSC_RPC_HTTPS"]:
        env_rpc = (os.getenv(key) or "").strip()
        if env_rpc:
            return env_rpc
    return ""


def load_okx_wallet_credentials() -> dict[str, str]:
    address = load_okx_wallet_address()
    private_key = ""
    for key in ["OKX_WALLET_PRIVATE_KEY", "BINANCE_BOT_WALLET_PRIVATE_KEY", "BASE_WALLET_PRIVATE_KEY"]:
        value = (os.getenv(key) or "").strip()
        if value:
            private_key = value
            break

    wallet_json = (os.getenv("BINANCE_BOT_WALLET_JSON") or "").strip()
    if wallet_json:
        try:
            payload = json.loads(Path(wallet_json).read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                address = str(payload.get("address") or address).strip()
                private_key = str(payload.get("private_key") or payload.get("privateKey") or private_key).strip()
        except Exception:
            pass

    return {"address": address, "private_key": private_key}


TOKEN_MANAGER_V2 = Web3.to_checksum_address("0x5c952063c7fc8610FFDB798152D69F0B9550762b")
TOKEN_HELPER_V3 = Web3.to_checksum_address("0xF251F83e40a78868FcfA3FA4599Dad6494E46034")

HELPER_ABI = [
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "getTokenInfo",
        "outputs": [
            {"name": "version", "type": "uint256"},
            {"name": "tokenManager", "type": "address"},
            {"name": "quote", "type": "address"},
            {"name": "lastPrice", "type": "uint256"},
            {"name": "tradingFeeRate", "type": "uint256"},
            {"name": "minTradingFee", "type": "uint256"},
            {"name": "launchTime", "type": "uint256"},
            {"name": "offers", "type": "uint256"},
            {"name": "maxOffers", "type": "uint256"},
            {"name": "funds", "type": "uint256"},
            {"name": "maxFunds", "type": "uint256"},
            {"name": "liquidityAdded", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "funds", "type": "uint256"},
        ],
        "name": "tryBuy",
        "outputs": [
            {"name": "tokenManager", "type": "address"},
            {"name": "quote", "type": "address"},
            {"name": "estimatedAmount", "type": "uint256"},
            {"name": "estimatedCost", "type": "uint256"},
            {"name": "estimatedFee", "type": "uint256"},
            {"name": "amountMsgValue", "type": "uint256"},
            {"name": "amountApproval", "type": "uint256"},
            {"name": "amountFunds", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "trySell",
        "outputs": [
            {"name": "tokenManager", "type": "address"},
            {"name": "quote", "type": "address"},
            {"name": "funds", "type": "uint256"},
            {"name": "fee", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

TM2_ABI = [
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "funds", "type": "uint256"},
            {"name": "minAmount", "type": "uint256"},
        ],
        "name": "buyTokenAMAP",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "sellToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


ERC20_META_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class OnchainBscTrader:
    def __init__(self):
        self.ready = False
        self.wallet_address = ""
        self._w3 = None
        self._helper = None
        self._tm2 = None
        self._private_key = ""
        self.last_error = ""
        self.last_attempt_ts = 0
        self.rpc_url = ""
        self._init_retries = max(1, int(os.getenv("BINANCE_BOT_ONCHAIN_INIT_RETRIES", "3") or "3"))
        self._init_retry_sec = max(1, int(os.getenv("BINANCE_BOT_ONCHAIN_INIT_RETRY_SEC", "2") or "2"))
        self._recheck_sec = max(5, int(os.getenv("BINANCE_BOT_ONCHAIN_RECHECK_SEC", "30") or "30"))
        self._attempt_init(force=True)

    def _log(self, message: str) -> None:
        logger = globals().get("log")
        if callable(logger):
            logger(message)
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    def _attempt_init(self, force: bool = False) -> bool:
        now_ts = int(time.time())
        if self.ready and not force:
            return True
        if not force and self.last_attempt_ts and now_ts - self.last_attempt_ts < self._recheck_sec:
            return bool(self.ready)

        self.last_attempt_ts = now_ts
        self.ready = False
        self.wallet_address = ""
        self._w3 = None
        self._helper = None
        self._tm2 = None
        self._private_key = ""
        self.last_error = ""
        self.rpc_url = ""

        creds = load_okx_wallet_credentials()
        private_key = str((creds or {}).get("private_key") or "").strip()
        if not private_key:
            self.last_error = "missing private key"
            return False

        rpc = load_okx_bsc_rpc()
        if not rpc:
            self.last_error = "missing BSC RPC"
            return False
        self.rpc_url = rpc

        last_error = ""
        for attempt in range(1, self._init_retries + 1):
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
                if not w3.is_connected():
                    last_error = f"rpc not connected attempt={attempt}"
                else:
                    chain_id = int(w3.eth.chain_id)
                    if chain_id != 56:
                        last_error = f"unexpected chain_id={chain_id} attempt={attempt}"
                    else:
                        account = w3.eth.account.from_key(private_key)
                        self.wallet_address = account.address
                        self._private_key = private_key
                        self._w3 = w3
                        self._helper = w3.eth.contract(address=TOKEN_HELPER_V3, abi=HELPER_ABI)
                        self._tm2 = w3.eth.contract(address=TOKEN_MANAGER_V2, abi=TM2_ABI)
                        self.ready = True
                        self.last_error = ""
                        if attempt > 1:
                            self._log(f"[INFO] onchain 交易器恢复成功 attempt={attempt} rpc={rpc}")
                        return True
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:220]}"
                if _is_timeout_error(e):
                    send_timeout_telegram_alert("onchain_init_rpc", str(e), cooldown_sec=600)

            if attempt < self._init_retries:
                time.sleep(self._init_retry_sec)

        self.last_error = last_error or "unknown init failure"
        return False

    def ensure_ready(self, force: bool = False) -> bool:
        if self._attempt_init(force=force):
            return True
        if self.last_error:
            self._log(f"[WARN] onchain 交易器未就绪: {self.last_error}")
        return False

    def _pending_nonce(self) -> int:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        return int(self._w3.eth.get_transaction_count(self.wallet_address, "pending"))

    def _wait_receipt(self, tx_hash: str, timeout_sec: int = 180) -> dict[str, Any]:
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_sec)
        status = int(getattr(receipt, "status", 0) or 0)
        block_number = int(getattr(receipt, "blockNumber", 0) or 0)
        gas_used = int(getattr(receipt, "gasUsed", 0) or 0)
        if status != 1:
            raise RuntimeError(f"tx_failed:{tx_hash}")
        return {
            "status": status,
            "blockNumber": block_number,
            "gasUsed": gas_used,
        }

    def buy(self, contract_address: str, quote_usdt: float, bnb_price_usdt: float, slippage_bps: int, min_sellback_ratio: float, max_entry_loss_usdt: float, dry_run: bool) -> dict[str, Any]:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        if bnb_price_usdt <= 0:
            raise RuntimeError("invalid BNBUSDT price")

        token = Web3.to_checksum_address(contract_address)
        funds_bnb = float(quote_usdt) / float(bnb_price_usdt)
        funds_wei = int(Web3.to_wei(Decimal(str(funds_bnb)), "ether"))

        info = self._helper.functions.getTokenInfo(token).call()
        quote_addr = info[2]
        if int(quote_addr, 16) != 0:
            raise RuntimeError("token quote is not BNB")

        estimate = self._helper.functions.tryBuy(token, 0, funds_wei).call()
        estimated_amount = int(estimate[2])
        if estimated_amount <= 0:
            raise RuntimeError("estimated amount zero")

        sell_try = self._helper.functions.trySell(token, estimated_amount).call()
        sell_quote = sell_try[1]
        sell_funds = int(sell_try[2])
        if int(sell_quote, 16) != 0:
            raise RuntimeError("trySell quote is not BNB")
        if sell_funds <= 0:
            raise RuntimeError("trySell funds is zero")

        sellback_ratio = float(sell_funds) / float(funds_wei) if funds_wei > 0 else 0.0
        if sellback_ratio < float(min_sellback_ratio):
            raise RuntimeError(f"sellback_ratio_too_low:{sellback_ratio:.4f}<{float(min_sellback_ratio):.4f}")

        entry_loss_quote = max(0.0, float(quote_usdt) * (1.0 - sellback_ratio))
        if max_entry_loss_usdt >= 0 and entry_loss_quote > float(max_entry_loss_usdt):
            raise RuntimeError(f"daily_loss_guard:{entry_loss_quote:.4f}>{float(max_entry_loss_usdt):.4f}")

        msg_value = int(estimate[5]) if int(estimate[5]) > 0 else funds_wei
        min_amount = int(estimated_amount * (10000 - int(slippage_bps)) / 10000)

        if dry_run:
            return {
                "dryRun": True,
                "route": "onchain",
                "wallet": self.wallet_address,
                "contract": token,
                "fundsBnb": funds_bnb,
                "fundsWei": funds_wei,
                "estimatedAmount": str(estimated_amount),
                "sellbackRatio": round(sellback_ratio, 6),
                "entryLossQuote": round(entry_loss_quote, 6),
            }

        nonce = self._pending_nonce()
        gas_price = int(self._w3.eth.gas_price * 1.1)
        tx = self._tm2.functions.buyTokenAMAP(token, funds_wei, min_amount).build_transaction(
            {
                "from": self.wallet_address,
                "value": msg_value,
                "nonce": nonce,
                "chainId": 56,
                "gasPrice": gas_price,
            }
        )
        if "gas" not in tx:
            tx["gas"] = int(self._w3.eth.estimate_gas(tx) * 1.2)

        signed = self._w3.eth.account.sign_transaction(tx, private_key=self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        receipt = self._wait_receipt(tx_hash)
        return {
            "route": "onchain",
            "wallet": self.wallet_address,
            "contract": token,
            "fundsBnb": funds_bnb,
            "fundsWei": funds_wei,
            "estimatedAmount": str(estimated_amount),
            "sellbackRatio": round(sellback_ratio, 6),
            "entryLossQuote": round(entry_loss_quote, 6),
            "txHash": tx_hash,
            "receipt": receipt,
            "engine": "helper",
        }

    def broadcast_prebuilt_tx(self, tx_payload: dict[str, Any], dry_run: bool, route_label: str = "okx-onchain") -> dict[str, Any]:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        if not isinstance(tx_payload, dict) or not tx_payload:
            raise RuntimeError("invalid prebuilt tx payload")

        tx = {
            "from": self.wallet_address,
            "to": Web3.to_checksum_address(str(tx_payload.get("to") or "")),
            "data": str(tx_payload.get("data") or "0x"),
            "value": int(tx_payload.get("value") or 0),
            "gas": int(tx_payload.get("gas") or 0),
            "nonce": self._pending_nonce(),
            "chainId": 56,
        }
        gas_price = int(tx_payload.get("gasPrice") or 0)
        max_priority = int(tx_payload.get("maxPriorityFeePerGas") or 0)
        max_fee = int(tx_payload.get("maxFeePerGas") or 0)
        if max_fee > 0:
            tx["maxFeePerGas"] = max_fee
            tx["maxPriorityFeePerGas"] = max_priority if max_priority > 0 else max_fee
        else:
            tx["gasPrice"] = gas_price if gas_price > 0 else int(self._w3.eth.gas_price * 1.1)
        if tx["gas"] <= 0:
            est = dict(tx)
            est.pop("nonce", None)
            tx["gas"] = int(self._w3.eth.estimate_gas(est) * 1.2)

        if dry_run:
            return {
                "dryRun": True,
                "route": route_label,
                "wallet": self.wallet_address,
                "to": tx["to"],
                "value": str(tx["value"]),
                "gas": str(tx["gas"]),
                "engine": "okx",
            }

        signed = self._w3.eth.account.sign_transaction(tx, private_key=self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        receipt = self._wait_receipt(tx_hash)
        return {
            "route": route_label,
            "wallet": self.wallet_address,
            "to": tx["to"],
            "value": str(tx["value"]),
            "gas": str(tx["gas"]),
            "txHash": tx_hash,
            "receipt": receipt,
            "engine": "okx",
        }

    def buy_via_okx(self, contract_address: str, quote_usdt: float, bnb_price_usdt: float, slippage_bps: int, dry_run: bool, max_price_impact_pct: float, timeout_sec: int, min_sellback_ratio: float, max_entry_loss_usdt: float) -> dict[str, Any]:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        swap = okx_build_buy_swap(
            token_contract=contract_address,
            quote_usdt=quote_usdt,
            bnb_price_usdt=bnb_price_usdt,
            wallet_address=self.wallet_address,
            chain="bsc",
            slippage_pct=max(0.1, float(slippage_bps) / 100.0),
            max_price_impact_pct=max_price_impact_pct,
            timeout_sec=timeout_sec,
        )
        if not swap.ok:
            raise RuntimeError(swap.reason or swap.status)
        sellback_ratio = max(0.0, 1.0 - max(0.0, float(swap.price_impact_pct)) / 100.0 - max(0.0, float(swap.tax_rate)) / 100.0)
        if sellback_ratio < float(min_sellback_ratio):
            raise RuntimeError(f"sellback_ratio_too_low:{sellback_ratio:.4f}<{float(min_sellback_ratio):.4f}")
        entry_loss_quote = max(0.0, float(quote_usdt) * (1.0 - sellback_ratio) + float(swap.trade_fee_usd))
        if max_entry_loss_usdt >= 0 and entry_loss_quote > float(max_entry_loss_usdt):
            raise RuntimeError(f"daily_loss_guard:{entry_loss_quote:.4f}>{float(max_entry_loss_usdt):.4f}")
        resp = self.broadcast_prebuilt_tx(swap.tx, dry_run=dry_run, route_label="okx-onchain")
        resp.update({
            "contract": Web3.to_checksum_address(contract_address),
            "estimatedAmount": str(swap.min_receive_amount),
            "sellbackRatio": round(sellback_ratio, 6),
            "entryLossQuote": round(entry_loss_quote, 6),
            "okxRouteCount": int(swap.route_count),
            "okxPriceImpact": round(float(swap.price_impact_pct), 4),
            "okxTradeFeeUsd": round(float(swap.trade_fee_usd), 6),
            "okxHoneypot": bool(swap.honeypot),
            "okxTaxRate": round(float(swap.tax_rate), 6),
        })
        return resp

    def estimate_sell_funds_wei_okx(self, contract_address: str, amount_raw: int, timeout_sec: int = 12, max_price_impact_pct: float = 12.0) -> int:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        quote = okx_quote_sell_token(
            token_contract=contract_address,
            amount_raw=amount_raw,
            wallet_address=self.wallet_address,
            chain="bsc",
            timeout_sec=timeout_sec,
            max_price_impact_pct=max_price_impact_pct,
        )
        if not quote.ok:
            raise RuntimeError(quote.reason or quote.status)
        to_amt = int((quote.raw or {}).get("toTokenAmount") or 0)
        return to_amt

    def sell_via_okx(self, contract_address: str, amount_raw: int, slippage_bps: int, dry_run: bool, timeout_sec: int, max_price_impact_pct: float) -> dict[str, Any]:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        swap = okx_build_sell_swap(
            token_contract=contract_address,
            amount_raw=amount_raw,
            wallet_address=self.wallet_address,
            chain="bsc",
            slippage_pct=max(0.1, float(slippage_bps) / 100.0),
            max_price_impact_pct=max_price_impact_pct,
            timeout_sec=timeout_sec,
        )
        if not swap.ok:
            raise RuntimeError(swap.reason or swap.status)
        resp = self.broadcast_prebuilt_tx(swap.tx, dry_run=dry_run, route_label="okx-onchain")
        resp.update({
            "contract": Web3.to_checksum_address(contract_address),
            "amountRaw": str(max(0, int(amount_raw))),
            "okxRouteCount": int(swap.route_count),
            "okxPriceImpact": round(float(swap.price_impact_pct), 4),
            "okxTradeFeeUsd": round(float(swap.trade_fee_usd), 6),
            "okxHoneypot": bool(swap.honeypot),
            "okxTaxRate": round(float(swap.tax_rate), 6),
        })
        return resp

    def native_balance_wei(self) -> int:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        try:
            return int(self._w3.eth.get_balance(self.wallet_address))
        except Exception as e:
            raise RuntimeError(str(e)[:220])

    def native_balance_bnb(self) -> float:
        return float(Web3.from_wei(self.native_balance_wei(), "ether"))

    def token_balance_raw(self, contract_address: str) -> int:
        if not self.ensure_ready():
            return 0
        try:
            token = Web3.to_checksum_address(contract_address)
            token_c = self._w3.eth.contract(address=token, abi=ERC20_META_ABI)
            return int(token_c.functions.balanceOf(self.wallet_address).call())
        except Exception:
            return 0

    def estimate_sell_funds_wei(self, contract_address: str, amount_raw: int) -> int:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")
        token = Web3.to_checksum_address(contract_address)
        qty = max(0, int(amount_raw))
        if qty <= 0:
            return 0
        sell_try = self._helper.functions.trySell(token, qty).call()
        sell_quote = sell_try[1]
        sell_funds = int(sell_try[2])
        if int(sell_quote, 16) != 0:
            raise RuntimeError("trySell quote is not BNB")
        if sell_funds <= 0:
            return 0
        return sell_funds

    def sell(self, contract_address: str, amount_raw: int, dry_run: bool) -> dict[str, Any]:
        if not self.ensure_ready():
            raise RuntimeError(f"onchain trader not ready: {self.last_error or 'unknown'}")

        token = Web3.to_checksum_address(contract_address)
        amount = max(0, int(amount_raw))
        if amount <= 0:
            raise RuntimeError("sell amount zero")

        token_c = self._w3.eth.contract(address=token, abi=ERC20_META_ABI)
        allowance = int(token_c.functions.allowance(self.wallet_address, TOKEN_MANAGER_V2).call())

        approve_tx_hash = ""
        if allowance < amount:
            nonce = self._pending_nonce()
            gas_price = int(self._w3.eth.gas_price * 1.1)
            tx_approve = token_c.functions.approve(TOKEN_MANAGER_V2, amount).build_transaction(
                {
                    "from": self.wallet_address,
                    "nonce": nonce,
                    "chainId": 56,
                    "gasPrice": gas_price,
                }
            )
            if "gas" not in tx_approve:
                tx_approve["gas"] = int(self._w3.eth.estimate_gas(tx_approve) * 1.2)

            if dry_run:
                return {
                    "dryRun": True,
                    "route": "onchain",
                    "wallet": self.wallet_address,
                    "contract": token,
                    "amountRaw": str(amount),
                    "needApprove": True,
                    "sellTxHash": "",
                    "approveTxHash": "",
                }

            signed_approve = self._w3.eth.account.sign_transaction(tx_approve, private_key=self._private_key)
            approve_tx_hash = self._w3.eth.send_raw_transaction(signed_approve.raw_transaction).hex()
            self._wait_receipt(approve_tx_hash, timeout_sec=120)
        elif dry_run:
            return {
                "dryRun": True,
                "route": "onchain",
                "wallet": self.wallet_address,
                "contract": token,
                "amountRaw": str(amount),
                "needApprove": False,
                "sellTxHash": "",
                "approveTxHash": "",
            }

        nonce = self._pending_nonce()
        gas_price = int(self._w3.eth.gas_price * 1.1)
        tx_sell = self._tm2.functions.sellToken(token, amount).build_transaction(
            {
                "from": self.wallet_address,
                "nonce": nonce,
                "chainId": 56,
                "gasPrice": gas_price,
            }
        )
        if "gas" not in tx_sell:
            tx_sell["gas"] = int(self._w3.eth.estimate_gas(tx_sell) * 1.2)

        signed_sell = self._w3.eth.account.sign_transaction(tx_sell, private_key=self._private_key)
        sell_tx_hash = self._w3.eth.send_raw_transaction(signed_sell.raw_transaction).hex()
        receipt = self._wait_receipt(sell_tx_hash)
        return {
            "route": "onchain",
            "wallet": self.wallet_address,
            "contract": token,
            "amountRaw": str(amount),
            "needApprove": allowance < amount,
            "approveTxHash": approve_tx_hash,
            "sellTxHash": sell_tx_hash,
            "receipt": receipt,
        }


@dataclass
class Config:
    enabled: bool
    dry_run: bool
    testnet: bool
    binance_api_key: str
    binance_api_secret: str
    quote_asset: str
    max_usdt_per_trade: float
    max_daily_usdt: float
    max_daily_loss_usdt: float
    min_score: float
    take_profit_pct: float
    stop_loss_pct: float
    poll_interval_sec: float
    signal_chain_id: str
    max_candidates_per_loop: int
    rank_pages: int
    rank_page_size: int
    follow_wallet_addresses: list[str]
    follow_wallet_top_n: int
    smart_wallet_auto_collect: bool
    smart_wallet_auto_limit: int
    smart_wallet_cache_sec: int
    news_enabled: bool
    news_limit: int
    news_cache_sec: int
    news_max_age_sec: int
    square_news_enabled: bool
    square_news_limit: int
    square_news_cache_sec: int
    square_news_max_age_sec: int
    square_news_cookie_header: str
    square_news_csrf_token: str
    square_news_session_token: str
    watch_address: str
    mode: str
    okx_quote_probe_enabled: bool
    okx_quote_probe_timeout_sec: int
    okx_quote_probe_max_price_impact_pct: float
    onchain_okx_primary_enabled: bool
    onchain_slippage_bps: int
    onchain_min_sellback_ratio: float
    onchain_zero_amount_cooldown_sec: int
    onchain_min_bnb_reserve: float
    onchain_max_wallet_usage_per_trade: float
    onchain_min_launch_age_minutes: int
    onchain_max_launch_age_minutes: int
    onchain_min_entry_liquidity_usdt: float
    onchain_max_entry_liquidity_usdt: float
    onchain_min_entry_holders: int
    onchain_max_entry_holders: int
    risk_sizing_enabled: bool
    risk_sizing_min_quote_usdt: float
    risk_sizing_max_multiplier: float
    dynamic_min_score_enabled: bool
    dynamic_min_score_floor: float
    fallback_quote_usdt: float
    onchain_sell_enabled: bool
    onchain_take_profit_pct: float
    onchain_stop_loss_pct: float
    onchain_trailing_stop_pct: float
    onchain_panic_drop_5m_pct: float
    onchain_panic_drop_1h_pct: float
    onchain_min_hold_seconds: int
    onchain_max_hold_minutes: int
    onchain_tp_partial_ratio: float
    onchain_tp_second_multiplier: float
    onchain_stagnation_sell_enabled: bool
    onchain_stagnation_min_hold_minutes: int
    onchain_stagnation_low_liq_hold_minutes: int
    onchain_stagnation_liq_threshold: float
    onchain_stagnation_holder_threshold: int
    onchain_stagnation_max_volume_1h_usdt: float
    onchain_stagnation_max_volume_5m_usdt: float
    onchain_stagnation_max_abs_change_1h_pct: float
    onchain_stagnation_max_abs_change_5m_pct: float
    onchain_stagnation_max_pnl_pct: float
    onchain_loss_streak_block: int
    onchain_hard_block_keywords: list[str]
    auto_evolve_enabled: bool
    auto_evolve_apply_state: bool
    auto_evolve_interval_sec: int
    auto_evolve_window_minutes: int
    auto_evolve_min_sell_events: int
    auto_evolve_model: str
    auto_evolve_log_tail_chars: int
    auto_evolve_candidate_sample_size: int
    auto_evolve_live_source_refresh: bool
    onchain_block_contracts: list[str]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_log_file() -> Path:
    custom = (os.getenv("BINANCE_BOT_LOG_FILE") or "").strip()
    return Path(custom) if custom else DEFAULT_LOG_FILE


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now_str()}] {msg}"
    if sys.stdout.isatty():
        print(line)
    lf = current_log_file()
    lf.parent.mkdir(parents=True, exist_ok=True)
    with lf.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_text_tail(path: Path, max_chars: int = 8000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


def extract_json_blob(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", raw)
        raw = re.sub(r"\n```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


_TIMEOUT_ALERT_TS: dict[str, int] = {}


def openclaw_bin_path() -> str:
    path = (os.getenv("OPENCLAW_BIN") or "/opt/homebrew/bin/openclaw").strip()
    return path or "/opt/homebrew/bin/openclaw"


def send_telegram_alert(message_text: str) -> bool:
    if not as_bool(os.getenv("BINANCE_BOT_TELEGRAM_ALERT_ENABLED"), True):
        return False

    target = (os.getenv("BINANCE_BOT_TELEGRAM_TARGET") or "").strip()
    if not target:
        log("[WARN] Telegram提醒跳过：未配置 BINANCE_BOT_TELEGRAM_TARGET")
        return False

    openclaw_bin = openclaw_bin_path()

    cmd = [
        openclaw_bin,
        "message",
        "send",
        "--channel",
        "telegram",
        "--json",
        "--target",
        target,
        "--message",
        str(message_text or "").strip(),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        )
    except Exception as e:
        log(f"[WARN] Telegram提醒异常: {str(e)[:120]}")
        return False

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or out or "").strip()
        log(f"[WARN] Telegram提醒失败 exit={proc.returncode} err={err[:160]}")
        return False

    ok = False
    if out:
        try:
            payload = json.loads(out)
            if isinstance(payload, dict):
                if "ok" in payload:
                    ok = bool(payload.get("ok"))
                elif isinstance(payload.get("payload"), dict):
                    ok = bool(payload["payload"].get("ok"))
        except Exception:
            ok = '"ok":true' in out.replace(" ", "").lower()

    if ok:
        log("[INFO] Telegram提醒发送成功")
        return True

    log(f"[WARN] Telegram提醒返回异常: {out[:200]}")
    return False


def _is_timeout_error(err: Any) -> bool:
    text = str(err or "").strip().lower()
    if not text:
        return False
    tokens = [
        "timed out",
        "timeout",
        "readtimeout",
        "connecttimeout",
        "httpsconnectionpool",
        "read timed out",
        "max retries exceeded",
    ]
    return any(tok in text for tok in tokens)


def call_openclaw_agent_text(prompt: str, timeout: int = 300, session_id: str = "") -> tuple[str | None, str | None]:
    timeout = max(60, min(300, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_TIMEOUT_SEC"), timeout)))
    sid = (session_id or os.getenv("BINANCE_BOT_AUTO_EVOLVE_SESSION_ID") or "binance-auto-evolve-worker").strip()
    thinking = (os.getenv("BINANCE_BOT_AUTO_EVOLVE_THINKING") or "high").strip().lower()
    if thinking not in {"off", "minimal", "low", "medium", "high"}:
        thinking = "high"

    cmd = [
        openclaw_bin_path(),
        "agent",
        "--agent",
        "main",
        "--session-id",
        sid,
        "--json",
        "--timeout",
        str(max(60, int(timeout))),
        "--thinking",
        thinking,
        "--message",
        prompt,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(90, int(timeout) + 20),
            env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        )
    except Exception as e:
        return None, f"openclaw_agent_exec_failed:{str(e)[:180]}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        return None, f"openclaw_agent_cli_{proc.returncode}:{err[:180]}"

    raw = (proc.stdout or "").strip()
    if not raw:
        return None, "openclaw_agent_empty_stdout"

    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}\s*$", raw)
        if not m:
            return None, "openclaw_agent_invalid_json"
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None, "openclaw_agent_invalid_json"

    payloads = (((data.get("result") or {}).get("payloads")) or [])
    for item in payloads:
        text = (item or {}).get("text")
        if isinstance(text, str) and text.strip():
            return text.strip(), None
    return None, "openclaw_agent_no_text"


def send_timeout_telegram_alert(source: str, detail: str, cooldown_sec: int | None = None) -> bool:
    source_key = str(source or "unknown").strip() or "unknown"
    cooldown = max(60, to_int(cooldown_sec, os.getenv("BINANCE_BOT_TIMEOUT_ALERT_COOLDOWN_SEC") or 900))
    now_ts = int(time.time())
    last_ts = int(_TIMEOUT_ALERT_TS.get(source_key, 0) or 0)
    if now_ts - last_ts < cooldown:
        return False
    _TIMEOUT_ALERT_TS[source_key] = now_ts

    text = (
        "⚠️ 币安自动交易脚本 API 超时\n"
        f"来源: {source_key}\n"
        f"时间: {now_str()}\n"
        f"详情: {str(detail or '')[:180]}"
    )
    return send_telegram_alert(text)


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def as_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def split_csv_words(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        text = str(part or "").strip()
        if not text:
            continue
        out.append(text)
    return out


SQUARE_TEXT_FIELDS = (
    "bodyTextOnly",
    "bodyText",
    "body",
    "content",
    "text",
    "title",
    "summary",
    "description",
    "brief",
)
SQUARE_TS_FIELDS = (
    "createTime",
    "publishTime",
    "postTime",
    "updateTime",
    "timestamp",
    "time",
)
SQUARE_SYMBOL_STOPWORDS = {
    "USD",
    "USDT",
    "USDC",
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "THE",
    "AND",
    "FOR",
    "WITH",
    "THIS",
    "THAT",
    "FROM",
    "YOUR",
    "BINANCE",
    "SQUARE",
    "NEWS",
    "ALPHA",
}
SQUARE_MARKER_SYMBOL_RE = re.compile(r"[$#]([A-Za-z][A-Za-z0-9]{1,11})")
SQUARE_PAIR_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,10})/(?:USDT|USD|BTC|BNB|ETH|SOL)\b")


def _normalize_symbol(text: Any) -> str:
    sym = str(text or "").strip().upper()
    if not sym or len(sym) > 12:
        return ""
    if sym in SQUARE_SYMBOL_STOPWORDS:
        return ""
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", sym):
        return ""
    return sym


def _collect_square_symbols_from_text(text: Any) -> list[str]:
    raw = str(text or "")
    out: list[str] = []
    seen: set[str] = set()

    for match in SQUARE_MARKER_SYMBOL_RE.findall(raw):
        sym = _normalize_symbol(match)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    for match in SQUARE_PAIR_SYMBOL_RE.findall(raw):
        sym = _normalize_symbol(match)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    return out


def _looks_like_square_item(obj: dict[str, Any]) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in ("contentId", "postId", "id", "tokenList", "coinList", *SQUARE_TEXT_FIELDS):
        if key in obj:
            return True
    return False


def _walk_square_items(value: Any, out: list[dict[str, Any]], seen_ids: set[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, list):
        for item in value[:500]:
            _walk_square_items(item, out, seen_ids, depth + 1)
        return
    if not isinstance(value, dict):
        return

    if _looks_like_square_item(value):
        item_id = str(value.get("contentId") or value.get("postId") or value.get("id") or "").strip()
        dedupe_key = item_id or f"anon:{len(out)}:{abs(hash(str(sorted(value.keys()))))}"
        if dedupe_key not in seen_ids:
            seen_ids.add(dedupe_key)
            out.append(value)

    for child in value.values():
        if isinstance(child, (list, dict)):
            _walk_square_items(child, out, seen_ids, depth + 1)


def _square_item_timestamp_ms(item: dict[str, Any]) -> int:
    for key in SQUARE_TS_FIELDS:
        val = to_int(item.get(key), 0)
        if val <= 0:
            continue
        if val < 10_000_000_000:
            return val * 1000
        return val
    return 0


def _square_item_symbols(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for key in ("tokenList", "coinList", "relatedTokens", "symbols"):
        value = item.get(key)
        if isinstance(value, list):
            for entry in value[:50]:
                if isinstance(entry, dict):
                    for field in ("symbol", "ticker", "tokenSymbol", "coinSymbol"):
                        sym = _normalize_symbol(entry.get(field))
                        if sym and sym not in seen:
                            seen.add(sym)
                            out.append(sym)
                else:
                    sym = _normalize_symbol(entry)
                    if sym and sym not in seen:
                        seen.add(sym)
                        out.append(sym)

    for field in SQUARE_TEXT_FIELDS:
        for sym in _collect_square_symbols_from_text(item.get(field)):
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)

    return out


def load_config() -> Config:
    load_env_file(WORKSPACE / "scripts" / ".env.secrets")
    load_env_file(WORKSPACE / "scripts" / ".env.workers")
    load_env_file(ROOT / ".env")

    watch_address = (os.getenv("BINANCE_BOT_WATCH_ADDRESS") or "").strip()
    if not watch_address:
        watch_address = load_okx_wallet_address()

    mode = (os.getenv("BINANCE_BOT_MODE") or "all").strip().lower()
    if mode not in {"all", "signals", "trade", "positions"}:
        mode = "all"

    follow_wallet_addresses: list[str] = []
    for raw in (os.getenv("BINANCE_BOT_SMART_WALLET_ADDRESSES") or "").split(","):
        addr = str(raw or "").strip()
        if not addr:
            continue
        if not ADDR_RE.fullmatch(addr):
            continue
        try:
            follow_wallet_addresses.append(Web3.to_checksum_address(addr))
        except Exception:
            continue

    onchain_block_contracts: list[str] = []
    for raw in (os.getenv("BINANCE_BOT_ONCHAIN_BLOCK_CONTRACTS") or "").split(","):
        addr = str(raw or "").strip()
        if not addr:
            continue
        if not ADDR_RE.fullmatch(addr):
            continue
        onchain_block_contracts.append(addr.lower())

    onchain_hard_block_keywords = split_csv_words(
        os.getenv("BINANCE_BOT_ONCHAIN_HARD_BLOCK_KEYWORDS") or "貔貅,honeypot,pixiu"
    )

    return Config(
        enabled=as_bool(os.getenv("BINANCE_BOT_ENABLED"), True),
        dry_run=as_bool(os.getenv("BINANCE_BOT_DRY_RUN"), True),
        testnet=as_bool(os.getenv("BINANCE_BOT_TESTNET"), False),
        binance_api_key=(os.getenv("BINANCE_API_KEY") or "").strip(),
        binance_api_secret=(os.getenv("BINANCE_API_SECRET") or "").strip(),
        quote_asset=(os.getenv("BINANCE_BOT_QUOTE_ASSET") or "USDT").upper(),
        max_usdt_per_trade=to_float(os.getenv("BINANCE_BOT_MAX_USDT_PER_TRADE"), 20.0),
        max_daily_usdt=to_float(os.getenv("BINANCE_BOT_MAX_DAILY_USDT"), 20.0),
        max_daily_loss_usdt=max(0.0, to_float(os.getenv("BINANCE_BOT_MAX_DAILY_LOSS_USDT"), 0.0)),
        min_score=to_float(os.getenv("BINANCE_BOT_MIN_SCORE"), 55.0),
        take_profit_pct=to_float(os.getenv("BINANCE_BOT_TAKE_PROFIT_PCT"), 2.0),
        stop_loss_pct=to_float(os.getenv("BINANCE_BOT_STOP_LOSS_PCT"), -1.5),
        poll_interval_sec=max(0.5, to_float(os.getenv("BINANCE_BOT_POLL_INTERVAL_SEC"), 30.0)),
        signal_chain_id=(os.getenv("BINANCE_BOT_SIGNAL_CHAIN_ID") or "56").strip(),
        max_candidates_per_loop=to_int(os.getenv("BINANCE_BOT_MAX_CANDIDATES"), 0),
        rank_pages=max(1, to_int(os.getenv("BINANCE_BOT_RANK_PAGES"), 4)),
        rank_page_size=max(20, min(200, to_int(os.getenv("BINANCE_BOT_RANK_PAGE_SIZE"), 120))),
        follow_wallet_addresses=follow_wallet_addresses,
        follow_wallet_top_n=max(1, to_int(os.getenv("BINANCE_BOT_SMART_WALLET_TOP_N"), 10)),
        smart_wallet_auto_collect=as_bool(os.getenv("BINANCE_BOT_SMART_WALLET_AUTO_COLLECT"), True),
        smart_wallet_auto_limit=max(1, min(100, to_int(os.getenv("BINANCE_BOT_SMART_WALLET_AUTO_LIMIT"), 20))),
        smart_wallet_cache_sec=max(60, to_int(os.getenv("BINANCE_BOT_SMART_WALLET_CACHE_SEC"), 600)),
        news_enabled=as_bool(os.getenv("BINANCE_BOT_NEWS_ENABLED"), True),
        news_limit=max(5, min(200, to_int(os.getenv("BINANCE_BOT_NEWS_LIMIT"), 30))),
        news_cache_sec=max(30, to_int(os.getenv("BINANCE_BOT_NEWS_CACHE_SEC"), 180)),
        news_max_age_sec=max(30, to_int(os.getenv("BINANCE_BOT_NEWS_MAX_AGE_SEC"), 900)),
        square_news_enabled=as_bool(os.getenv("BINANCE_BOT_SQUARE_NEWS_ENABLED"), False),
        square_news_limit=max(5, min(200, to_int(os.getenv("BINANCE_BOT_SQUARE_NEWS_LIMIT"), 20))),
        square_news_cache_sec=max(30, to_int(os.getenv("BINANCE_BOT_SQUARE_NEWS_CACHE_SEC"), 180)),
        square_news_max_age_sec=max(30, to_int(os.getenv("BINANCE_BOT_SQUARE_NEWS_MAX_AGE_SEC"), 1800)),
        square_news_cookie_header=(os.getenv("BINANCE_SQUARE_COOKIE_HEADER") or os.getenv("BINANCE_COOKIE_HEADER") or "").strip(),
        square_news_csrf_token=(os.getenv("BINANCE_SQUARE_CSRF_TOKEN") or os.getenv("BINANCE_CSRF_TOKEN") or "").strip(),
        square_news_session_token=(os.getenv("BINANCE_SQUARE_SESSION_TOKEN") or os.getenv("BINANCE_SESSION_TOKEN") or "").strip(),
        watch_address=watch_address,
        mode=mode,
        okx_quote_probe_enabled=as_bool(os.getenv("BINANCE_BOT_OKX_QUOTE_PROBE_ENABLED"), True),
        okx_quote_probe_timeout_sec=max(3, to_int(os.getenv("BINANCE_BOT_OKX_QUOTE_PROBE_TIMEOUT_SEC"), 12)),
        okx_quote_probe_max_price_impact_pct=max(0.1, to_float(os.getenv("BINANCE_BOT_OKX_QUOTE_PROBE_MAX_PRICE_IMPACT_PCT"), 8.0)),
        onchain_okx_primary_enabled=as_bool(os.getenv("BINANCE_BOT_ONCHAIN_OKX_PRIMARY_ENABLED"), True),
        onchain_slippage_bps=max(50, to_int(os.getenv("BINANCE_BOT_ONCHAIN_SLIPPAGE_BPS"), 900)),
        onchain_min_sellback_ratio=min(1.0, max(0.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_MIN_SELLBACK_RATIO"), 0.05))),
        onchain_zero_amount_cooldown_sec=max(60, to_int(os.getenv("BINANCE_BOT_ONCHAIN_ZERO_AMOUNT_COOLDOWN_SEC"), 900)),
        onchain_min_bnb_reserve=max(0.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_MIN_BNB_RESERVE"), 0.003)),
        onchain_max_wallet_usage_per_trade=min(1.0, max(0.01, to_float(os.getenv("BINANCE_BOT_ONCHAIN_MAX_WALLET_USAGE_PER_TRADE"), 0.25))),
        onchain_min_launch_age_minutes=max(0, to_int(os.getenv("BINANCE_BOT_ONCHAIN_MIN_LAUNCH_AGE_MINUTES"), 2)),
        onchain_max_launch_age_minutes=max(1, to_int(os.getenv("BINANCE_BOT_ONCHAIN_MAX_LAUNCH_AGE_MINUTES"), 180)),
        onchain_min_entry_liquidity_usdt=max(0.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_MIN_ENTRY_LIQUIDITY_USDT"), 5000.0)),
        onchain_max_entry_liquidity_usdt=max(1000.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_MAX_ENTRY_LIQUIDITY_USDT"), 150000.0)),
        onchain_min_entry_holders=max(0, to_int(os.getenv("BINANCE_BOT_ONCHAIN_MIN_ENTRY_HOLDERS"), 50)),
        onchain_max_entry_holders=max(1, to_int(os.getenv("BINANCE_BOT_ONCHAIN_MAX_ENTRY_HOLDERS"), 2000)),
        risk_sizing_enabled=as_bool(os.getenv("BINANCE_BOT_RISK_SIZING_ENABLED"), True),
        risk_sizing_min_quote_usdt=max(1.0, to_float(os.getenv("BINANCE_BOT_RISK_SIZING_MIN_QUOTE_USDT"), 10.0)),
        risk_sizing_max_multiplier=max(1.0, to_float(os.getenv("BINANCE_BOT_RISK_SIZING_MAX_MULTIPLIER"), 1.5)),
        dynamic_min_score_enabled=as_bool(os.getenv("BINANCE_BOT_DYNAMIC_MIN_SCORE_ENABLED"), True),
        dynamic_min_score_floor=to_float(os.getenv("BINANCE_BOT_DYNAMIC_MIN_SCORE_FLOOR"), 35.0),
        fallback_quote_usdt=max(10.0, to_float(os.getenv("BINANCE_BOT_FALLBACK_QUOTE_USDT"), 20.0)),
        onchain_sell_enabled=as_bool(os.getenv("BINANCE_BOT_ONCHAIN_SELL_ENABLED"), True),
        onchain_take_profit_pct=to_float(os.getenv("BINANCE_BOT_ONCHAIN_TAKE_PROFIT_PCT"), 35.0),
        onchain_stop_loss_pct=to_float(os.getenv("BINANCE_BOT_ONCHAIN_STOP_LOSS_PCT"), -18.0),
        onchain_trailing_stop_pct=max(1.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_TRAILING_STOP_PCT"), 16.0)),
        onchain_panic_drop_5m_pct=to_float(os.getenv("BINANCE_BOT_ONCHAIN_PANIC_DROP_5M_PCT"), -20.0),
        onchain_panic_drop_1h_pct=to_float(os.getenv("BINANCE_BOT_ONCHAIN_PANIC_DROP_1H_PCT"), -35.0),
        onchain_min_hold_seconds=max(0, to_int(os.getenv("BINANCE_BOT_ONCHAIN_MIN_HOLD_SECONDS"), 120)),
        onchain_max_hold_minutes=max(5, to_int(os.getenv("BINANCE_BOT_ONCHAIN_MAX_HOLD_MINUTES"), 120)),
        onchain_tp_partial_ratio=min(0.95, max(0.05, to_float(os.getenv("BINANCE_BOT_ONCHAIN_TP_PARTIAL_RATIO"), 0.6))),
        onchain_tp_second_multiplier=max(1.1, to_float(os.getenv("BINANCE_BOT_ONCHAIN_TP_SECOND_MULTIPLIER"), 1.8)),
        onchain_stagnation_sell_enabled=as_bool(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_SELL_ENABLED"), True),
        onchain_stagnation_min_hold_minutes=max(3, to_int(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_MIN_HOLD_MINUTES"), 18)),
        onchain_stagnation_low_liq_hold_minutes=max(2, to_int(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_LOW_LIQ_HOLD_MINUTES"), 8)),
        onchain_stagnation_liq_threshold=max(0.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_LIQ_THRESHOLD"), 6000.0)),
        onchain_stagnation_holder_threshold=max(0, to_int(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_HOLDER_THRESHOLD"), 120)),
        onchain_stagnation_max_volume_1h_usdt=max(0.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_MAX_VOLUME_1H_USDT"), 2500.0)),
        onchain_stagnation_max_volume_5m_usdt=max(0.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_MAX_VOLUME_5M_USDT"), 120.0)),
        onchain_stagnation_max_abs_change_1h_pct=max(0.1, to_float(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_MAX_ABS_CHANGE_1H_PCT"), 5.0)),
        onchain_stagnation_max_abs_change_5m_pct=max(0.1, to_float(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_MAX_ABS_CHANGE_5M_PCT"), 1.5)),
        onchain_stagnation_max_pnl_pct=max(-50.0, to_float(os.getenv("BINANCE_BOT_ONCHAIN_STAGNATION_MAX_PNL_PCT"), 10.0)),
        onchain_loss_streak_block=max(1, to_int(os.getenv("BINANCE_BOT_ONCHAIN_LOSS_STREAK_BLOCK"), 2)),
        onchain_hard_block_keywords=onchain_hard_block_keywords,
        auto_evolve_enabled=as_bool(os.getenv("BINANCE_BOT_AUTO_EVOLVE_ENABLED"), True),
        auto_evolve_apply_state=as_bool(os.getenv("BINANCE_BOT_AUTO_EVOLVE_APPLY_STATE"), False),
        auto_evolve_interval_sec=max(1800, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_INTERVAL_SEC"), 28800)),
        auto_evolve_window_minutes=max(60, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_WINDOW_MINUTES"), 480)),
        auto_evolve_min_sell_events=max(1, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_MIN_SELL_EVENTS"), 3)),
        auto_evolve_model=(os.getenv("BINANCE_BOT_AUTO_EVOLVE_MODEL") or "gpt-5.4").strip(),
        auto_evolve_log_tail_chars=max(4000, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_LOG_TAIL_CHARS"), 12000)),
        auto_evolve_candidate_sample_size=max(5, min(60, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_CANDIDATE_SAMPLE_SIZE"), 20))),
        auto_evolve_live_source_refresh=as_bool(os.getenv("BINANCE_BOT_AUTO_EVOLVE_LIVE_SOURCE_REFRESH"), True),
        onchain_block_contracts=onchain_block_contracts,
    )


class BinanceSpotClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_url = "https://testnet.binance.vision" if cfg.testnet else "https://api.binance.com"

    def _sign(self, params: dict[str, Any]) -> str:
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
        sig = hmac.new(
            self.cfg.binance_api_secret.encode("utf-8"),
            qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{qs}&signature={sig}"

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = requests.get(
                f"{self.base_url}{path}",
                params=params or {},
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if _is_timeout_error(e):
                send_timeout_telegram_alert(f"binance_spot_public_get:{path}", str(e))
            raise

    def signed_request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if not self.cfg.binance_api_key or not self.cfg.binance_api_secret:
            raise RuntimeError("缺少 BINANCE_API_KEY / BINANCE_API_SECRET")

        payload = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        query = self._sign(payload)

        headers = {"X-MBX-APIKEY": self.cfg.binance_api_key}
        url = f"{self.base_url}{path}?{query}"
        try:
            r = requests.request(method=method, url=url, headers=headers, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if _is_timeout_error(e):
                send_timeout_telegram_alert(f"binance_spot_signed:{method}:{path}", str(e))
            raise


class BinanceSkillsHubClient:
    def __init__(self):
        self.base = "https://web3.binance.com"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept-Encoding": "identity",
                "User-Agent": "Mozilla/5.0",
                "clienttype": "web",
                "clientversion": os.getenv("BINANCE_WEB3_CLIENT_VERSION", "1.2.0"),
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://web3.binance.com",
                "Referer": "https://web3.binance.com/",
            }
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = self.session.get(f"{self.base}{path}", params=params or {}, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if _is_timeout_error(e):
                send_timeout_telegram_alert(f"binance_web3_get:{path}", str(e))
            raise

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            r = self.session.post(
                f"{self.base}{path}",
                json=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if _is_timeout_error(e):
                send_timeout_telegram_alert(f"binance_web3_post:{path}", str(e))
            raise

    def _extract_data(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return payload.get("data")
        return payload

    # 1) trading-signal
    def smart_money_signals(self, chain_id: str = "56", page_size: int = 50) -> list[dict[str, Any]]:
        payload = self._post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money",
            {"smartSignalType": "", "page": 1, "pageSize": page_size, "chainId": chain_id},
        )
        data = self._extract_data(payload)
        return data if isinstance(data, list) else []

    # 2) crypto-market-rank
    def unified_rank(self, chain_id: str = "56", rank_type: int = 10, size: int = 20, page: int = 1) -> list[dict[str, Any]]:
        payload = self._post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list",
            {
                "rankType": rank_type,
                "chainId": chain_id,
                "period": 50,
                "sortBy": 70,
                "orderAsc": False,
                "page": page,
                "size": size,
            },
        )
        data = self._extract_data(payload)
        if isinstance(data, dict) and isinstance(data.get("tokens"), list):
            return data.get("tokens") or []
        if isinstance(data, list):
            return data
        return []

    # 3) meme-rush
    def meme_rush(self, chain_id: str = "56", rank_type: int = 20, limit: int = 30) -> list[dict[str, Any]]:
        payload = self._post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list",
            {"chainId": chain_id, "rankType": rank_type, "limit": limit},
        )
        data = self._extract_data(payload)
        if isinstance(data, dict) and isinstance(data.get("tokens"), list):
            return data.get("tokens") or []
        if isinstance(data, list):
            return data
        return []

    # 4) topic-rush (meme-rush 的另一能力)
    def topic_rush(self, chain_id: str = "56", rank_type: int = 20) -> list[dict[str, Any]]:
        payload = self._get(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list",
            {"chainId": chain_id, "rankType": rank_type, "sort": 10, "asc": False},
        )
        data = self._extract_data(payload)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data.get("list") or []
        return []

    # 5) query-token-info
    def token_search(self, keyword: str, chain_ids: str = "56") -> list[dict[str, Any]]:
        payload = self._get(
            "/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search",
            {"keyword": keyword, "chainIds": chain_ids, "orderBy": "volume24h"},
        )
        data = self._extract_data(payload)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data.get("list") or []
        return []

    def token_dynamic(self, chain_id: str, contract_address: str) -> dict[str, Any]:
        payload = self._get(
            "/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info",
            {"chainId": chain_id, "contractAddress": contract_address},
        )
        data = self._extract_data(payload)
        return data if isinstance(data, dict) else {}

    # 6) query-token-audit
    def token_audit(self, chain_id: str, contract_address: str) -> dict[str, Any]:
        payload = self._post(
            "/bapi/defi/v1/public/wallet-direct/security/token/audit",
            {
                "binanceChainId": chain_id,
                "contractAddress": contract_address,
                "requestId": str(uuid.uuid4()),
            },
        )
        data = self._extract_data(payload)
        return data if isinstance(data, dict) else {}

    # 7) query-address-info
    def address_positions(self, chain_id: str, address: str) -> list[dict[str, Any]]:
        try:
            payload = self._get(
                "/bapi/defi/v3/public/wallet-direct/buw/wallet/address/pnl/active-position-list",
                {"address": address, "chainId": chain_id, "offset": 0},
            )
        except Exception:
            return []

        if isinstance(payload, dict):
            success = payload.get("success")
            code = str(payload.get("code") or "").strip()
            data = payload.get("data")
            if success is False:
                return []
            if code and code not in {"0", "000000", "SUCCESS"} and data in (None, {}, []):
                return []

        data = self._extract_data(payload)
        if isinstance(data, dict):
            if isinstance(data.get("list"), list):
                return data.get("list") or []
            if isinstance(data.get("positions"), list):
                return data.get("positions") or []
        if isinstance(data, list):
            return data
        return []

    def pnl_leaderboard(self, chain_id: str = "56", page_no: int = 1, page_size: int = 25, tag: str = "ALL") -> list[dict[str, Any]]:
        payload = self._get(
            "/bapi/defi/v1/public/wallet-direct/market/leaderboard/query",
            {
                "tag": tag,
                "pageNo": page_no,
                "chainId": chain_id,
                "pageSize": page_size,
                "sortBy": 0,
                "orderBy": 0,
                "period": "30d",
            },
        )
        data = self._extract_data(payload)
        if isinstance(data, dict):
            if isinstance(data.get("data"), list):
                return data.get("data") or []
            if isinstance(data.get("list"), list):
                return data.get("list") or []
        if isinstance(data, list):
            return data
        return []


class BinanceSquareClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = "https://www.binance.com"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "identity",
                "Origin": "https://www.binance.com",
                "Referer": "https://www.binance.com/en/square",
                "clienttype": "web",
                "Content-Type": "application/json",
            }
        )
        if self.cfg.square_news_cookie_header:
            self.session.headers["Cookie"] = self.cfg.square_news_cookie_header
        if self.cfg.square_news_csrf_token:
            self.session.headers["x-csrf-token"] = self.cfg.square_news_csrf_token
        if self.cfg.square_news_session_token:
            self.session.headers["x-session-token"] = self.cfg.square_news_session_token

    def can_read(self) -> bool:
        return bool(self.cfg.square_news_cookie_header)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            r = self.session.post(f"{self.base}{path}", json=body, timeout=20)
        except Exception as e:
            if _is_timeout_error(e):
                send_timeout_telegram_alert(f"binance_square_post:{path}", str(e))
            raise

        content_type = str(r.headers.get("content-type") or "").lower()
        text = r.text or ""
        if r.status_code >= 400:
            if r.status_code == 403 and "<!doctype html" in text.lower():
                raise RuntimeError(f"square_feed_blocked_403:{path}")
            raise RuntimeError(f"square_http_{r.status_code}:{path}:{text[:160]}")
        if "application/json" not in content_type and not text.strip().startswith("{"):
            raise RuntimeError(f"square_non_json:{path}:{text[:120]}")
        return r.json()

    def fetch_items(self, limit: int = 20) -> list[dict[str, Any]]:
        attempts: list[tuple[str, list[dict[str, Any]]]] = [
            (
                "/bapi/composite/v1/public/pgc/content/recommend/list",
                [
                    {"limit": limit, "offset": 0},
                    {"page": 1, "size": limit},
                    {"pageNo": 1, "pageSize": limit},
                ],
            ),
            (
                "/bapi/composite/v1/public/pgc/content/discover/list",
                [
                    {"limit": limit, "offset": 0},
                    {"page": 1, "size": limit},
                    {"pageNo": 1, "pageSize": limit},
                ],
            ),
            (
                "/bapi/composite/v1/public/pgc/content/hot/list",
                [
                    {"limit": limit, "offset": 0},
                    {"page": 1, "size": limit},
                    {"pageNo": 1, "pageSize": limit},
                ],
            ),
            (
                "/bapi/composite/v1/public/pgc/feed/list",
                [
                    {"limit": limit, "offset": 0},
                    {"page": 1, "size": limit},
                    {"pageNo": 1, "pageSize": limit},
                ],
            ),
            (
                "/bapi/composite/v1/public/pgc/timeline/list",
                [
                    {"limit": limit, "offset": 0},
                    {"page": 1, "size": limit},
                    {"pageNo": 1, "pageSize": limit},
                ],
            ),
            (
                "/bapi/composite/v1/public/pgc/content/list",
                [
                    {"page": 1, "size": limit},
                    {"pageNo": 1, "pageSize": limit},
                    {"limit": limit, "offset": 0},
                ],
            ),
        ]

        last_error = ""
        for path, bodies in attempts:
            for body in bodies:
                try:
                    payload = self._post(path, body)
                    items: list[dict[str, Any]] = []
                    _walk_square_items(payload, items, set())
                    if items:
                        return items[: max(1, limit * 4)]
                except Exception as e:
                    last_error = str(e)[:220]
                    if "illegal parameter" in last_error.lower():
                        continue
                    if "square_feed_blocked_403" in last_error:
                        continue
                    continue
        if last_error:
            raise RuntimeError(last_error)
        return []


class StrategyEngine:
    def __init__(self, cfg: Config, spot: BinanceSpotClient, skills: BinanceSkillsHubClient):
        self.cfg = cfg
        self.spot = spot
        self.skills = skills
        self.square = BinanceSquareClient(cfg)
        self.state = self._load_state()
        self.exchange_map = self._load_exchange_symbols()
        self.onchain_trader = OnchainBscTrader()
        if not self.onchain_trader.ready and self.onchain_trader.last_error:
            log(f"[WARN] onchain 交易器初始化失败: {self.onchain_trader.last_error}")
        self._news_cache_ts = 0
        self._news_cache_symbols: list[str] = []
        self._square_news_cache_ts = 0
        self._square_news_cache_symbols: list[str] = []
        self._square_news_warned = False
        self._smart_wallet_cache_ts = 0
        self._smart_wallet_cache_addresses: list[str] = []
        self._orphan_reconcile_ts = 0
        self._orphan_reconcile_interval_sec = max(30, to_int(os.getenv("BINANCE_BOT_ONCHAIN_RECONCILE_INTERVAL_SEC"), 120))
        self._orphan_reconcile_min_usdt = max(0.5, to_float(os.getenv("BINANCE_BOT_ONCHAIN_RECONCILE_MIN_USDT"), 1.0))
        self._orphan_reconcile_max_items = max(1, to_int(os.getenv("BINANCE_BOT_ONCHAIN_RECONCILE_MAX_ITEMS"), 12))
        self._orphan_reconcile_scan_lines = max(10000, to_int(os.getenv("BINANCE_BOT_ONCHAIN_RECONCILE_SCAN_LINES"), 80000))
        self._onchain_block_contracts = {str(x or "").strip().lower() for x in self.cfg.onchain_block_contracts if str(x or "").strip()}
        self._hard_block_keywords = {str(x or "").strip().lower() for x in self.cfg.onchain_hard_block_keywords if str(x or "").strip()}
        self._last_source_stats: dict[str, Any] = {"candidates": 0, "news_tokens": 0, "follow_tokens": 0}
        self._onchain_timeout_cooldown_sec = max(120, to_int(os.getenv("BINANCE_BOT_ONCHAIN_TIMEOUT_COOLDOWN_SEC"), 1800))

        hard_blocks_from_state = self.state.get("hard_block_contracts")
        if isinstance(hard_blocks_from_state, list):
            for addr in hard_blocks_from_state:
                text = str(addr or "").strip().lower()
                if ADDR_RE.fullmatch(text):
                    self._onchain_block_contracts.add(text)

        self._apply_auto_evolve_from_state()

    def _load_state(self) -> dict[str, Any]:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "day": "",
            "spent": 0.0,
            "loss": 0.0,
            "positions": {},
            "position_manage_cursor": 0,
            "zero_amount_until": {},
            "zero_amount_hits": {},
            "hard_block_contracts": [],
            "auto_evolve": {},
        }
        state_file = current_state_file(self.cfg.dry_run)
        source_file = state_file if state_file.exists() else (STATE_FILE if STATE_FILE.exists() else state_file)
        if source_file.exists():
            try:
                loaded = json.loads(source_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
            except Exception:
                pass

        if not isinstance(state.get("positions"), dict):
            state["positions"] = {}
        state["position_manage_cursor"] = max(0, to_int(state.get("position_manage_cursor"), 0))

        filtered_positions: dict[str, Any] = {}
        for key, pos in (state.get("positions") or {}).items():
            if not isinstance(pos, dict):
                continue
            if source_file == STATE_FILE and str(pos.get("route") or "") == "spot" and "dry_run_position" not in pos:
                continue
            if bool(pos.get("dry_run_position", False)) != bool(self.cfg.dry_run):
                continue
            filtered_positions[str(key)] = pos
        state["positions"] = filtered_positions
        if not isinstance(state.get("zero_amount_until"), dict):
            state["zero_amount_until"] = {}
        if not isinstance(state.get("zero_amount_hits"), dict):
            state["zero_amount_hits"] = {}
        if not isinstance(state.get("hard_block_contracts"), list):
            state["hard_block_contracts"] = []
        if not isinstance(state.get("auto_evolve"), dict):
            state["auto_evolve"] = {}

        cleaned_zero: dict[str, int] = {}
        cleaned_zero_hits: dict[str, int] = {}
        now_ts = int(time.time())
        for k, v in (state.get("zero_amount_until") or {}).items():
            key = str(k).strip().lower()
            until = to_int(v, 0)
            if key and until > now_ts:
                cleaned_zero[key] = until
                cleaned_zero_hits[key] = max(0, to_int((state.get("zero_amount_hits") or {}).get(key), 0))
        state["zero_amount_until"] = cleaned_zero
        state["zero_amount_hits"] = cleaned_zero_hits

        clean_blocks: list[str] = []
        for addr in (state.get("hard_block_contracts") or []):
            text = str(addr or "").strip().lower()
            if ADDR_RE.fullmatch(text):
                clean_blocks.append(text)
        state["hard_block_contracts"] = sorted(set(clean_blocks))

        state["spent"] = max(0.0, to_float(state.get("spent"), 0.0))
        state["loss"] = max(0.0, to_float(state.get("loss"), 0.0))
        state["day"] = str(state.get("day") or "")
        return state

    def _apply_auto_evolve_from_state(self) -> None:
        if not self.cfg.auto_evolve_apply_state:
            log("[EVOLVE] state-restore 已禁用，保留当前手动/env 参数")
            return

        meta = self.state.get("auto_evolve")
        if not isinstance(meta, dict):
            return

        params = meta.get("params")
        if not isinstance(params, dict):
            return

        self._apply_param_overrides(params, reason="state-restore")

    def _apply_param_overrides(self, params: dict[str, Any], reason: str = "auto") -> dict[str, tuple[Any, Any]]:
        mappings = {
            "min_score": ("min_score", lambda v: max(8.0, min(80.0, to_float(v, self.cfg.min_score)))),
            "dynamic_min_score_floor": ("dynamic_min_score_floor", lambda v: max(5.0, min(70.0, to_float(v, self.cfg.dynamic_min_score_floor)))),
            "max_daily_loss_usdt": ("max_daily_loss_usdt", lambda v: max(0.01, min(200.0, to_float(v, self.cfg.max_daily_loss_usdt)))),
            "risk_sizing_min_quote_usdt": ("risk_sizing_min_quote_usdt", lambda v: max(1.0, min(50.0, to_float(v, self.cfg.risk_sizing_min_quote_usdt)))),
            "risk_sizing_max_multiplier": ("risk_sizing_max_multiplier", lambda v: max(1.0, min(4.0, to_float(v, self.cfg.risk_sizing_max_multiplier)))),
            "onchain_zero_amount_cooldown_sec": ("onchain_zero_amount_cooldown_sec", lambda v: max(60, min(1800, to_int(v, self.cfg.onchain_zero_amount_cooldown_sec)))),
            "onchain_min_hold_seconds": ("onchain_min_hold_seconds", lambda v: max(0, min(7200, to_int(v, self.cfg.onchain_min_hold_seconds)))),
            "onchain_max_hold_minutes": ("onchain_max_hold_minutes", lambda v: max(8, min(240, to_int(v, self.cfg.onchain_max_hold_minutes)))),
            "onchain_take_profit_pct": ("onchain_take_profit_pct", lambda v: max(5.0, min(300.0, to_float(v, self.cfg.onchain_take_profit_pct)))),
            "onchain_stop_loss_pct": ("onchain_stop_loss_pct", lambda v: min(-1.0, max(-90.0, to_float(v, self.cfg.onchain_stop_loss_pct)))),
            "onchain_trailing_stop_pct": ("onchain_trailing_stop_pct", lambda v: max(1.0, min(90.0, to_float(v, self.cfg.onchain_trailing_stop_pct)))),
            "onchain_panic_drop_5m_pct": ("onchain_panic_drop_5m_pct", lambda v: min(-1.0, max(-90.0, to_float(v, self.cfg.onchain_panic_drop_5m_pct)))),
            "onchain_panic_drop_1h_pct": ("onchain_panic_drop_1h_pct", lambda v: min(-1.0, max(-95.0, to_float(v, self.cfg.onchain_panic_drop_1h_pct)))),
            "onchain_tp_partial_ratio": ("onchain_tp_partial_ratio", lambda v: max(0.05, min(0.95, to_float(v, self.cfg.onchain_tp_partial_ratio)))),
            "onchain_tp_second_multiplier": ("onchain_tp_second_multiplier", lambda v: max(1.1, min(5.0, to_float(v, self.cfg.onchain_tp_second_multiplier)))),
            "onchain_stagnation_min_hold_minutes": ("onchain_stagnation_min_hold_minutes", lambda v: max(3, min(90, to_int(v, self.cfg.onchain_stagnation_min_hold_minutes)))),
            "onchain_stagnation_low_liq_hold_minutes": ("onchain_stagnation_low_liq_hold_minutes", lambda v: max(2, min(120, to_int(v, self.cfg.onchain_stagnation_low_liq_hold_minutes)))),
            "onchain_stagnation_liq_threshold": ("onchain_stagnation_liq_threshold", lambda v: max(200.0, min(200000.0, to_float(v, self.cfg.onchain_stagnation_liq_threshold)))),
            "onchain_stagnation_holder_threshold": ("onchain_stagnation_holder_threshold", lambda v: max(0, min(200000, to_int(v, self.cfg.onchain_stagnation_holder_threshold)))),
            "onchain_stagnation_max_volume_1h_usdt": ("onchain_stagnation_max_volume_1h_usdt", lambda v: max(0.0, min(500000.0, to_float(v, self.cfg.onchain_stagnation_max_volume_1h_usdt)))),
            "onchain_stagnation_max_volume_5m_usdt": ("onchain_stagnation_max_volume_5m_usdt", lambda v: max(0.0, min(100000.0, to_float(v, self.cfg.onchain_stagnation_max_volume_5m_usdt)))),
            "onchain_stagnation_max_abs_change_1h_pct": ("onchain_stagnation_max_abs_change_1h_pct", lambda v: max(0.1, min(80.0, to_float(v, self.cfg.onchain_stagnation_max_abs_change_1h_pct)))),
            "onchain_stagnation_max_abs_change_5m_pct": ("onchain_stagnation_max_abs_change_5m_pct", lambda v: max(0.1, min(50.0, to_float(v, self.cfg.onchain_stagnation_max_abs_change_5m_pct)))),
            "onchain_stagnation_max_pnl_pct": ("onchain_stagnation_max_pnl_pct", lambda v: max(-50.0, min(120.0, to_float(v, self.cfg.onchain_stagnation_max_pnl_pct)))),
            "onchain_min_sellback_ratio": ("onchain_min_sellback_ratio", lambda v: max(0.0, min(0.2, to_float(v, self.cfg.onchain_min_sellback_ratio)))),
        }

        changed: dict[str, tuple[Any, Any]] = {}
        for key, value in params.items():
            item = mappings.get(str(key))
            if not item:
                continue
            attr, caster = item
            old_val = getattr(self.cfg, attr)
            new_val = caster(value)
            if isinstance(old_val, float):
                if abs(float(new_val) - float(old_val)) < 1e-9:
                    continue
            elif new_val == old_val:
                continue
            setattr(self.cfg, attr, new_val)
            changed[attr] = (old_val, new_val)

        if changed:
            summary = ", ".join(f"{k}:{v[0]}->{v[1]}" for k, v in changed.items())
            log(f"[EVOLVE] 参数更新({reason}): {summary}")
        return changed

    def _remember_hard_block_contract(self, contract: str, token: str, reason: str) -> None:
        addr = str(contract or "").strip().lower()
        if not ADDR_RE.fullmatch(addr):
            return
        if addr in self._onchain_block_contracts:
            return

        self._onchain_block_contracts.add(addr)
        blocks = self.state.get("hard_block_contracts")
        if not isinstance(blocks, list):
            blocks = []
        blocks.append(addr)
        dedup = sorted({str(x or "").strip().lower() for x in blocks if ADDR_RE.fullmatch(str(x or "").strip().lower())})
        self.state["hard_block_contracts"] = dedup
        log(f"[BLOCK] 新增硬禁止合约 token={token} contract={addr} reason={reason}")

    def _token_name_hard_block_reason(self, token_name: str) -> str:
        name = str(token_name or "").strip().lower()
        if not name:
            return ""

        for kw in self._hard_block_keywords:
            if kw and kw in name:
                return f"keyword:{kw}"

        stock_map_keywords = {
            "etf", "stock", "stocks", "equity", "index", "nasdaq", "s&p", "dow", "gold", "silver", "oil",
            "美股", "股票", "股指", "指数", "纳指", "标普", "道指", "黄金", "白银", "原油",
            "tesla", "nvidia", "apple", "microsoft", "google", "amazon", "meta", "palantir", "coinbase",
        }
        for kw in stock_map_keywords:
            if kw in name:
                return f"asset_class:{kw}"

        upper = str(token_name or "").strip().upper()
        stock_mapped_bases = {
            "SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "PLTR",
            "MSTR", "COIN", "BABA", "HOOD", "INTC", "ORCL", "MU", "SLV", "IAU", "CRCL",
        }
        for base in stock_mapped_bases:
            if upper == f"{base}ON" or upper == base:
                return f"asset_class:{base}"

        return ""

    def _extract_audit_hard_block_reason(self, audit: dict[str, Any]) -> str:
        if not isinstance(audit, dict):
            return ""

        extra = audit.get("extraInfo")
        if isinstance(extra, dict):
            if as_bool(str(extra.get("isReported")), False):
                return "audit:reported"
            if as_bool(str(extra.get("unusualSellTax")), False) or as_bool(str(extra.get("unusualBuyTax")), False):
                return "audit:unusual_tax"

        risk_items = audit.get("riskItems")
        if not isinstance(risk_items, list):
            return ""

        for item in risk_items:
            details = (item or {}).get("details")
            if not isinstance(details, list):
                continue
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                if not bool(detail.get("isHit")):
                    continue
                title = str(detail.get("title") or "").strip().lower()
                if not title:
                    continue
                if "honeypot" in title:
                    return "audit:honeypot"
                if "rug pull" in title:
                    return "audit:rug_pull"
                if "scam" in title:
                    return "audit:scam"
                if "fake token" in title:
                    return "audit:fake_token"
                if "spam risk" in title:
                    return "audit:spam"
        return ""

    def _collect_recent_trade_stats(self, window_minutes: int) -> dict[str, Any]:
        out = {
            "buy_onchain": 0,
            "sell_onchain": 0,
            "sell_win": 0,
            "sell_loss": 0,
            "score_skip": 0,
            "estimated_zero": 0,
            "quote_not_bnb": 0,
            "loss_add_total": 0.0,
            "avg_liq_win": 0.0,
            "avg_liq_loss": 0.0,
            "avg_pnl_win": 0.0,
            "avg_pnl_loss": 0.0,
        }
        log_file = current_log_file()
        if not log_file.exists():
            return out

        now_ts = int(time.time())
        window_sec = max(300, int(window_minutes) * 60)

        try:
            with log_file.open("r", encoding="utf-8", errors="ignore") as f:
                lines = list(deque(f, maxlen=120000))
        except Exception:
            return out

        win_liq: list[float] = []
        loss_liq: list[float] = []
        win_pnl: list[float] = []
        loss_pnl: list[float] = []

        for raw in lines:
            m_ts = re.search(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", raw)
            if m_ts:
                try:
                    ts = int(datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
                    if now_ts - ts > window_sec:
                        continue
                except Exception:
                    pass

            if "[BUY-ONCHAIN]" in raw and '"dryRun": true' not in raw:
                out["buy_onchain"] += 1
            if "[SKIP]" in raw and "score=" in raw and "< min=" in raw:
                out["score_skip"] += 1
            if "过滤estimated_amount_zero" in raw:
                out["estimated_zero"] += 1
            if "token quote is not BNB" in raw:
                out["quote_not_bnb"] += 1
            if "[LOSS] route=onchain" in raw and "add=" in raw:
                m_loss = re.search(r"add=([0-9]+(?:\.[0-9]+)?)", raw)
                if m_loss:
                    out["loss_add_total"] += max(0.0, to_float(m_loss.group(1), 0.0))

            if "[SELL-ONCHAIN]" in raw:
                out["sell_onchain"] += 1
                m_pnl = re.search(r"pnl=([-0-9.]+)%", raw)
                m_liq = re.search(r"\sliq=([0-9]+(?:\.[0-9]+)?)", raw)
                pnl = to_float(m_pnl.group(1), 0.0) if m_pnl else 0.0
                liq = to_float(m_liq.group(1), 0.0) if m_liq else 0.0
                if pnl > 0:
                    out["sell_win"] += 1
                    win_liq.append(liq)
                    win_pnl.append(pnl)
                else:
                    out["sell_loss"] += 1
                    loss_liq.append(liq)
                    loss_pnl.append(abs(pnl))

        if win_liq:
            out["avg_liq_win"] = sum(win_liq) / len(win_liq)
        if loss_liq:
            out["avg_liq_loss"] = sum(loss_liq) / len(loss_liq)
        if win_pnl:
            out["avg_pnl_win"] = sum(win_pnl) / len(win_pnl)
        if loss_pnl:
            out["avg_pnl_loss"] = sum(loss_pnl) / len(loss_pnl)
        return out

    def _summarize_positions_for_evolve(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        positions = self.state.get("positions") or {}
        now_ts = int(time.time())
        for key, pos in list(positions.items())[:200]:
            if not isinstance(pos, dict):
                continue
            out.append({
                "key": str(key),
                "route": str(pos.get("route") or ""),
                "token": str(pos.get("token") or ""),
                "contract": str(pos.get("contract") or ""),
                "opened_minutes_ago": round(max(0, now_ts - to_int(pos.get("opened_at"), now_ts)) / 60.0, 2),
                "score": round(to_float(pos.get("score"), 0.0), 2),
                "entry_quote_usdt": round(to_float(pos.get("entry_quote_usdt"), 0.0), 4),
                "peak_quote_usdt": round(to_float(pos.get("peak_quote_usdt"), 0.0), 4),
                "qty": round(to_float(pos.get("qty"), 0.0), 8),
                "riskLevel": to_int(pos.get("riskLevel"), 0),
                "liquidity": round(to_float(pos.get("liquidity"), 0.0), 2),
                "holders": to_int(pos.get("holders"), 0),
                "tp1_done": bool(pos.get("tp1_done", False)),
            })
        out.sort(key=lambda x: x.get("opened_minutes_ago", 0), reverse=True)
        return out[:40]

    def _sample_candidates_for_evolve(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for cand in candidates[: max(20, int(self.cfg.auto_evolve_candidate_sample_size) * 4)]:
            if not isinstance(cand, dict):
                continue
            score, detail = self._score_candidate(cand)
            rows.append({
                "symbol": str(cand.get("symbol") or ""),
                "contractAddress": str((cand.get("contractAddress") or detail.get("contract") or "")).strip(),
                "score": round(score, 2),
                "signal_count": to_int(cand.get("signal_count"), 0),
                "smart_money_count": to_int(cand.get("smart_money_count"), 0),
                "follow_wallet_count": to_int(cand.get("follow_wallet_count"), 0),
                "news_count": to_int(cand.get("news_count"), 0),
                "in_rank": bool(cand.get("in_rank")),
                "in_alpha": bool(cand.get("in_alpha")),
                "in_meme": bool(cand.get("in_meme")),
                "in_topic": bool(cand.get("in_topic")),
                "topic_net_inflow": round(to_float(cand.get("topic_net_inflow"), 0.0), 2),
                "liquidity": round(to_float(detail.get("liquidity"), 0.0), 2),
                "holders": to_int(detail.get("holders"), 0),
                "riskLevel": to_int(detail.get("riskLevel"), 0),
                "hard_block_reason": str(detail.get("hard_block_reason") or ""),
            })
        rows.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return rows[: int(self.cfg.auto_evolve_candidate_sample_size)]

    def _collect_recent_onchain_review_samples(self, window_minutes: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "recent_buys": [],
            "recent_sells": [],
            "winners": [],
            "losers": [],
        }
        log_file = current_log_file()
        if not log_file.exists():
            return out

        now_ts = int(time.time())
        window_sec = max(300, int(window_minutes) * 60)
        try:
            with log_file.open("r", encoding="utf-8", errors="ignore") as f:
                lines = list(deque(f, maxlen=120000))
        except Exception:
            return out

        for raw in lines:
            m_ts = re.search(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", raw)
            if m_ts:
                try:
                    ts = int(datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
                    if now_ts - ts > window_sec:
                        continue
                except Exception:
                    pass

            if "[BUY-ONCHAIN]" in raw:
                m = {
                    "token": re.search(r"token=([^\s]+)", raw),
                    "score": re.search(r"score=([-0-9.]+)", raw),
                    "quote": re.search(r"quote=([0-9.]+)", raw),
                    "contract": re.search(r"contract=(0x[a-fA-F0-9]{40})", raw),
                    "sellback": re.search(r"sellback=([0-9.]+)", raw),
                    "entry_loss": re.search(r"entry_loss=([0-9.]+)", raw),
                }
                item = {
                    "token": m["token"].group(1) if m["token"] else "",
                    "score": round(to_float(m["score"].group(1), 0.0), 2) if m["score"] else 0.0,
                    "quote": round(to_float(m["quote"].group(1), 0.0), 4) if m["quote"] else 0.0,
                    "contract": m["contract"].group(1) if m["contract"] else "",
                    "sellback": round(to_float(m["sellback"].group(1), 0.0), 4) if m["sellback"] else 0.0,
                    "entry_loss": round(to_float(m["entry_loss"].group(1), 0.0), 4) if m["entry_loss"] else 0.0,
                }
                out["recent_buys"].append(item)

            if "[SELL-ONCHAIN]" in raw:
                fields = {
                    "token": re.search(r"token=([^\s]+)", raw),
                    "contract": re.search(r"contract=(0x[a-fA-F0-9]{40})", raw),
                    "reason": re.search(r"reason=([^\s]+)", raw),
                    "pnl": re.search(r"pnl=([-0-9.]+)%", raw),
                    "drawdown": re.search(r"drawdown=([-0-9.]+)%", raw),
                    "est_quote": re.search(r"est_quote=([0-9.]+)", raw),
                    "entry_quote": re.search(r"entry_quote=([0-9.]+)", raw),
                    "ratio": re.search(r"ratio=([0-9.]+)", raw),
                    "risk": re.search(r"risk=([0-9]+)", raw),
                    "liq": re.search(r"\sliq=([0-9.]+)", raw),
                    "holders": re.search(r"holders=([0-9]+)", raw),
                    "chg5m": re.search(r"chg5m=([-0-9.]+)%", raw),
                    "chg1h": re.search(r"chg1h=([-0-9.]+)%", raw),
                    "vol5m": re.search(r"vol5m=([0-9.]+)", raw),
                    "vol1h": re.search(r"vol1h=([0-9.]+)", raw),
                }
                item = {
                    "token": fields["token"].group(1) if fields["token"] else "",
                    "contract": fields["contract"].group(1) if fields["contract"] else "",
                    "reason": fields["reason"].group(1) if fields["reason"] else "",
                    "pnl_pct": round(to_float(fields["pnl"].group(1), 0.0), 2) if fields["pnl"] else 0.0,
                    "drawdown_pct": round(to_float(fields["drawdown"].group(1), 0.0), 2) if fields["drawdown"] else 0.0,
                    "est_quote": round(to_float(fields["est_quote"].group(1), 0.0), 4) if fields["est_quote"] else 0.0,
                    "entry_quote": round(to_float(fields["entry_quote"].group(1), 0.0), 4) if fields["entry_quote"] else 0.0,
                    "ratio": round(to_float(fields["ratio"].group(1), 0.0), 2) if fields["ratio"] else 0.0,
                    "risk": to_int(fields["risk"].group(1), 0) if fields["risk"] else 0,
                    "liq": round(to_float(fields["liq"].group(1), 0.0), 2) if fields["liq"] else 0.0,
                    "holders": to_int(fields["holders"].group(1), 0) if fields["holders"] else 0,
                    "chg5m_pct": round(to_float(fields["chg5m"].group(1), 0.0), 2) if fields["chg5m"] else 0.0,
                    "chg1h_pct": round(to_float(fields["chg1h"].group(1), 0.0), 2) if fields["chg1h"] else 0.0,
                    "vol5m": round(to_float(fields["vol5m"].group(1), 0.0), 2) if fields["vol5m"] else 0.0,
                    "vol1h": round(to_float(fields["vol1h"].group(1), 0.0), 2) if fields["vol1h"] else 0.0,
                }
                out["recent_sells"].append(item)
                if item["pnl_pct"] > 0:
                    out["winners"].append(item)
                else:
                    out["losers"].append(item)

        out["recent_buys"] = out["recent_buys"][-20:]
        out["recent_sells"] = out["recent_sells"][-30:]
        out["winners"] = sorted(out["winners"], key=lambda x: x.get("pnl_pct", 0.0), reverse=True)[:12]
        out["losers"] = sorted(out["losers"], key=lambda x: x.get("pnl_pct", 0.0))[:12]
        return out

    def _enrich_contract_review_rows(self, rows: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            contract = str((row or {}).get("contract") or "").strip()
            if not ADDR_RE.fullmatch(contract):
                continue
            low = contract.lower()
            if low in seen:
                continue
            seen.add(low)
            item = dict(row)
            try:
                dyn = self.skills.token_dynamic(self.cfg.signal_chain_id, contract)
            except Exception:
                dyn = {}
            try:
                audit = self.skills.token_audit(self.cfg.signal_chain_id, contract)
            except Exception:
                audit = {}
            item["dynamic"] = {
                "percentChange5m": round(to_float(dyn.get("percentChange5m"), 0.0), 2),
                "percentChange1h": round(to_float(dyn.get("percentChange1h"), 0.0), 2),
                "volume5m": round(to_float(dyn.get("volume5m"), 0.0), 2),
                "volume1h": round(to_float(dyn.get("volume1h"), 0.0), 2),
                "liquidity": round(to_float(dyn.get("liquidity"), 0.0), 2),
                "holders": to_int(dyn.get("holders"), 0),
            }
            item["audit"] = {
                "riskLevel": to_int(audit.get("riskLevel"), 0),
                "hard_block_reason": self._extract_audit_hard_block_reason(audit),
            }
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def _build_llm_evolve_context(self) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        live_source_error = ""
        if self.cfg.auto_evolve_live_source_refresh:
            try:
                candidates = self._candidate_symbols()
            except Exception as e:
                live_source_error = str(e)[:180]
        if not candidates:
            try:
                cached = self._read_candidates_cache()
                if isinstance(cached, list):
                    candidates = cached
            except Exception as e:
                if not live_source_error:
                    live_source_error = str(e)[:180]

        watch_positions: list[dict[str, Any]] = []
        if self.cfg.watch_address:
            try:
                watch_positions = (self.skills.address_positions(self.cfg.signal_chain_id, self.cfg.watch_address) or [])[:20]
            except Exception as e:
                live_source_error = (live_source_error + f";watch={str(e)[:120]}").strip(";")[:220]

        config_snapshot = {
            "mode": self.cfg.mode,
            "dry_run": self.cfg.dry_run,
            "signal_chain_id": self.cfg.signal_chain_id,
            "quote_asset": self.cfg.quote_asset,
            "max_usdt_per_trade": self.cfg.max_usdt_per_trade,
            "max_daily_usdt": self.cfg.max_daily_usdt,
            "max_daily_loss_usdt": self.cfg.max_daily_loss_usdt,
            "min_score": self.cfg.min_score,
            "dynamic_min_score_enabled": self.cfg.dynamic_min_score_enabled,
            "dynamic_min_score_floor": self.cfg.dynamic_min_score_floor,
            "risk_sizing_enabled": self.cfg.risk_sizing_enabled,
            "risk_sizing_min_quote_usdt": self.cfg.risk_sizing_min_quote_usdt,
            "risk_sizing_max_multiplier": self.cfg.risk_sizing_max_multiplier,
            "onchain_slippage_bps": self.cfg.onchain_slippage_bps,
            "onchain_min_sellback_ratio": self.cfg.onchain_min_sellback_ratio,
            "onchain_zero_amount_cooldown_sec": self.cfg.onchain_zero_amount_cooldown_sec,
            "onchain_take_profit_pct": self.cfg.onchain_take_profit_pct,
            "onchain_stop_loss_pct": self.cfg.onchain_stop_loss_pct,
            "onchain_trailing_stop_pct": self.cfg.onchain_trailing_stop_pct,
            "onchain_panic_drop_5m_pct": self.cfg.onchain_panic_drop_5m_pct,
            "onchain_panic_drop_1h_pct": self.cfg.onchain_panic_drop_1h_pct,
            "onchain_min_hold_seconds": self.cfg.onchain_min_hold_seconds,
            "onchain_max_hold_minutes": self.cfg.onchain_max_hold_minutes,
            "onchain_tp_partial_ratio": self.cfg.onchain_tp_partial_ratio,
            "onchain_tp_second_multiplier": self.cfg.onchain_tp_second_multiplier,
            "onchain_stagnation_sell_enabled": self.cfg.onchain_stagnation_sell_enabled,
            "onchain_stagnation_min_hold_minutes": self.cfg.onchain_stagnation_min_hold_minutes,
            "onchain_stagnation_low_liq_hold_minutes": self.cfg.onchain_stagnation_low_liq_hold_minutes,
            "onchain_stagnation_liq_threshold": self.cfg.onchain_stagnation_liq_threshold,
            "onchain_stagnation_holder_threshold": self.cfg.onchain_stagnation_holder_threshold,
            "onchain_stagnation_max_volume_1h_usdt": self.cfg.onchain_stagnation_max_volume_1h_usdt,
            "onchain_stagnation_max_volume_5m_usdt": self.cfg.onchain_stagnation_max_volume_5m_usdt,
            "onchain_stagnation_max_abs_change_1h_pct": self.cfg.onchain_stagnation_max_abs_change_1h_pct,
            "onchain_stagnation_max_abs_change_5m_pct": self.cfg.onchain_stagnation_max_abs_change_5m_pct,
            "onchain_stagnation_max_pnl_pct": self.cfg.onchain_stagnation_max_pnl_pct,
            "news_enabled": self.cfg.news_enabled,
            "square_news_enabled": self.cfg.square_news_enabled,
        }

        candidate_sample = self._sample_candidates_for_evolve(candidates)
        review_samples = self._collect_recent_onchain_review_samples(self.cfg.auto_evolve_window_minutes)
        top_risers = sorted(candidate_sample, key=lambda x: x.get("score", 0.0), reverse=True)[:8]
        top_risk_tokens = sorted(candidate_sample, key=lambda x: (x.get("riskLevel", 0), -x.get("score", 0.0)), reverse=True)[:8]

        context = {
            "generated_at": now_str(),
            "objective": "以持续稳定盈利为目标，优先保本、控制回撤、避免过度交易与明显高风险代币。",
            "state": {
                "day": str(self.state.get("day") or ""),
                "spent": round(to_float(self.state.get("spent"), 0.0), 4),
                "loss": round(to_float(self.state.get("loss"), 0.0), 4),
                "positions_count": len(self.state.get("positions") or {}),
                "hard_block_contracts_count": len(self.state.get("hard_block_contracts") or []),
                "zero_amount_blocked_count": len(self.state.get("zero_amount_until") or {}),
            },
            "config": config_snapshot,
            "auto_evolve_state": self.state.get("auto_evolve") or {},
            "recent_trade_stats": self._collect_recent_trade_stats(self.cfg.auto_evolve_window_minutes),
            "last_source_stats": dict(self._last_source_stats),
            "positions": self._summarize_positions_for_evolve(),
            "watch_address": self.cfg.watch_address,
            "watch_address_positions": watch_positions,
            "live_source_error": live_source_error,
            "candidate_sample": candidate_sample,
            "current_top_candidates": top_risers,
            "current_high_risk_candidates": top_risk_tokens,
            "recent_onchain_review": review_samples,
            "winner_feature_sample": self._enrich_contract_review_rows(review_samples.get("winners") or [], limit=8),
            "loser_feature_sample": self._enrich_contract_review_rows(review_samples.get("losers") or [], limit=8),
            "recent_buy_feature_sample": self._enrich_contract_review_rows(review_samples.get("recent_buys") or [], limit=8),
            "recent_sell_feature_sample": self._enrich_contract_review_rows(review_samples.get("recent_sells") or [], limit=10),
            "log_tails": {
                "trade": read_text_tail(LOG_DIR / "binance-trade.log", self.cfg.auto_evolve_log_tail_chars),
                "signals": read_text_tail(LOG_DIR / "binance-signals.log", self.cfg.auto_evolve_log_tail_chars),
                "positions": read_text_tail(LOG_DIR / "binance-position-watch.log", self.cfg.auto_evolve_log_tail_chars),
            },
        }
        return context

    def _send_evolve_report(self, review_data: dict[str, Any], changed: dict[str, tuple[Any, Any]], error_text: str) -> None:
        lines = ["🧠 币安自动交易 8小时复盘报告"]
        if error_text:
            lines.append(f"状态：复盘失败")
            lines.append(f"错误：{error_text[:300]}")
        else:
            lines.append(f"状态：{str(review_data.get('decision') or ('adjust' if changed else 'hold')).strip()}")
            regime = str(review_data.get('market_regime') or '').strip()
            if regime:
                lines.append(f"市场判断：{regime}")
            summary = str(review_data.get('summary') or '').strip()
            if summary:
                lines.append(f"总结：{summary[:600]}")

        if changed:
            lines.append("参数调整：")
            for key, (old_val, new_val) in changed.items():
                lines.append(f"- {key}: {old_val} -> {new_val}")
        else:
            lines.append("参数调整：本轮无变更")

        reasons = review_data.get("reasoning_points") if isinstance(review_data.get("reasoning_points"), list) else []
        if reasons:
            lines.append("原因：")
            for item in reasons[:8]:
                text = str(item or "").strip()
                if text:
                    lines.append(f"- {text[:180]}")

        for title, field, limit in [
            ("赢家特征", "winner_traits", 4),
            ("亏损特征", "loser_traits", 4),
            ("暴涨特征", "pump_traits", 4),
            ("暴跌特征", "dump_traits", 4),
            ("快卖特征", "fast_exit_features", 4),
            ("留底仓特征", "moonbag_features", 4),
            ("买点特征", "buy_timing_features", 4),
            ("卖点特征", "sell_timing_features", 4),
            ("逻辑建议", "logic_updates", 4),
        ]:
            items = review_data.get(field) if isinstance(review_data.get(field), list) else []
            if items:
                lines.append(f"{title}：")
                for item in items[:limit]:
                    text = str(item or "").strip()
                    if text:
                        lines.append(f"- {text[:180]}")

        risks = review_data.get("risk_flags") if isinstance(review_data.get("risk_flags"), list) else []
        if risks:
            lines.append("风险提示：")
            for item in risks[:6]:
                text = str(item or "").strip()
                if text:
                    lines.append(f"- {text[:180]}")

        next_focus = review_data.get("next_focus") if isinstance(review_data.get("next_focus"), list) else []
        if next_focus:
            lines.append("后续关注：")
            for item in next_focus[:6]:
                text = str(item or "").strip()
                if text:
                    lines.append(f"- {text[:180]}")

        sent = send_telegram_alert("\n".join(lines)[:3900])
        log(f"[EVOLVE-LLM] Telegram复盘报告发送结果 sent={sent}")

    def _maybe_auto_evolve(self) -> None:
        if not self.cfg.auto_evolve_enabled:
            return

        meta = self.state.get("auto_evolve")
        if not isinstance(meta, dict):
            meta = {}

        last_ts = to_int(meta.get("last_ts"), 0)
        now_ts = int(time.time())
        if last_ts > 0 and now_ts - last_ts < int(self.cfg.auto_evolve_interval_sec):
            return

        log("[EVOLVE-LLM] 开始执行 8 小时复盘")
        context = self._build_llm_evolve_context()
        prompt = (
            "你是资深量化风控与链上交易策略复盘官。你的唯一目标是在保本、控制回撤、避免过度交易和明显高风险代币的前提下，"
            "给 Binance 自动交易脚本做 8 小时一次的综合参数复盘。\n"
            "你必须综合以下全部上下文：当前运行参数、近8小时交易日志、近8小时交易统计、当前持仓、候选源样本、新闻/热点源情况、"
            "脚本内可获取的钱包与来源快照、赢家/输家代币特征、近期买卖记录以及链上动态/审计特征。\n"
            "你必须回答并吸收到参数决策里的问题包括但不限于：\n"
            "- 盈利代币为什么盈利、亏损代币为什么亏损，它们有哪些共同特征；\n"
            "- 链上暴涨/暴跌代币有哪些特征；\n"
            "- 哪些币应当盈利后尽快卖出，哪些币可以先出本后保留仓位搏金狗；\n"
            "- 买入时机和卖出时机的特征分别是什么；\n"
            "- 还有哪些我没明确说出但应该纳入复盘的重要信息。\n"
            "禁止为了追求交易频率而降低风控；禁止建议任何会明显放大回撤的激进参数；目标是持续稳定盈利，而不是短时冲动收益。\n"
            "只输出 JSON，不要解释，不要 markdown，不要代码块。\n"
            "JSON schema:\n"
            "{\n"
            "  \"summary\": \"string\",\n"
            "  \"market_regime\": \"trend|range|risk_off|mixed\",\n"
            "  \"decision\": \"adjust|hold\",\n"
            "  \"confidence\": 0-100,\n"
            "  \"winner_traits\": [\"string\"],\n"
            "  \"loser_traits\": [\"string\"],\n"
            "  \"pump_traits\": [\"string\"],\n"
            "  \"dump_traits\": [\"string\"],\n"
            "  \"fast_exit_features\": [\"string\"],\n"
            "  \"moonbag_features\": [\"string\"],\n"
            "  \"buy_timing_features\": [\"string\"],\n"
            "  \"sell_timing_features\": [\"string\"],\n"
            "  \"logic_updates\": [\"string\"],\n"
            "  \"adjustments\": {\n"
            "    \"min_score\": number,\n"
            "    \"dynamic_min_score_floor\": number,\n"
            "    \"max_daily_loss_usdt\": number,\n"
            "    \"risk_sizing_min_quote_usdt\": number,\n"
            "    \"risk_sizing_max_multiplier\": number,\n"
            "    \"onchain_zero_amount_cooldown_sec\": integer,\n"
            "    \"onchain_min_hold_seconds\": integer,\n"
            "    \"onchain_max_hold_minutes\": integer,\n"
            "    \"onchain_take_profit_pct\": number,\n"
            "    \"onchain_stop_loss_pct\": number,\n"
            "    \"onchain_trailing_stop_pct\": number,\n"
            "    \"onchain_panic_drop_5m_pct\": number,\n"
            "    \"onchain_panic_drop_1h_pct\": number,\n"
            "    \"onchain_tp_partial_ratio\": number,\n"
            "    \"onchain_tp_second_multiplier\": number,\n"
            "    \"onchain_stagnation_min_hold_minutes\": integer,\n"
            "    \"onchain_stagnation_low_liq_hold_minutes\": integer,\n"
            "    \"onchain_stagnation_liq_threshold\": number,\n"
            "    \"onchain_stagnation_holder_threshold\": integer,\n"
            "    \"onchain_stagnation_max_volume_1h_usdt\": number,\n"
            "    \"onchain_stagnation_max_volume_5m_usdt\": number,\n"
            "    \"onchain_stagnation_max_abs_change_1h_pct\": number,\n"
            "    \"onchain_stagnation_max_abs_change_5m_pct\": number,\n"
            "    \"onchain_stagnation_max_pnl_pct\": number,\n"
            "    \"onchain_min_sellback_ratio\": number\n"
            "  },\n"
            "  \"reasoning_points\": [\"string\"],\n"
            "  \"risk_flags\": [\"string\"],\n"
            "  \"next_focus\": [\"string\"]\n"
            "}\n"
            "规则：\n"
            "1) 如果不建议调整，请将 decision 设为 hold，并将 adjustments 设为空对象。\n"
            "2) 只建议脚本内部已有参数，且必须偏稳健。\n"
            "3) 当近期亏损、胜率差、zero_amount 高频、卖出质量差时，应更偏保守；当稳定盈利且回撤可控时，可小幅放松，但不要激进。\n"
            "4) 你的输出会直接作用于实盘参数，请宁可少改，也不要乱改。\n"
            f"以下是 8 小时复盘上下文 JSON：\n{json.dumps(context, ensure_ascii=False)}"
        )

        response_text = ""
        review_data: dict[str, Any] = {}
        changed: dict[str, tuple[Any, Any]] = {}
        error_text = ""

        max_retry = max(1, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_RETRY"), 3))
        base_delay = max(3, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_RETRY_BASE_SEC"), 5))
        max_delay = max(base_delay, to_int(os.getenv("BINANCE_BOT_AUTO_EVOLVE_RETRY_MAX_SEC"), 60))

        def _should_retry(msg: str) -> bool:
            text = str(msg or "").strip().lower()
            if not text:
                return False
            return any(
                token in text
                for token in (
                    "rate limit",
                    "try again later",
                    "upstream",
                    "temporarily unavailable",
                    "overloaded",
                    "502",
                    "503",
                    "gateway",
                    "timeout",
                    "timed out",
                    "readtimeout",
                    "connecttimeout",
                    "max retries exceeded",
                )
            )

        for attempt in range(1, max_retry + 1):
            try:
                response_text, err = call_openclaw_agent_text(prompt, timeout=300)
                if err:
                    raise RuntimeError(err)
                review = extract_json_blob(response_text or "")
                if not isinstance(review, dict):
                    raise RuntimeError("llm_review_invalid_json")
                review_data = review
                adjustments = review.get("adjustments") if isinstance(review.get("adjustments"), dict) else {}
                if str(review.get("decision") or "hold").strip().lower() == "adjust" and adjustments:
                    changed = self._apply_param_overrides(adjustments, reason="openclaw-auto-evolve")
                else:
                    log("[EVOLVE-LLM] 本轮决策为 hold，保持当前参数")
                error_text = ""
                break
            except Exception as e:
                error_text = str(e)[:240]
                retryable = _should_retry(error_text) or _should_retry(response_text)
                if retryable and attempt < max_retry:
                    sleep_sec = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    log(
                        f"[EVOLVE-LLM] 复盘失败(可重试) attempt={attempt}/{max_retry} err={error_text} -> {sleep_sec}s 后重试"
                    )
                    time.sleep(sleep_sec)
                    continue
                log(f"[EVOLVE-LLM] 复盘失败: {error_text}")
                break

        params_snapshot = {
            "min_score": self.cfg.min_score,
            "dynamic_min_score_floor": self.cfg.dynamic_min_score_floor,
            "max_daily_loss_usdt": self.cfg.max_daily_loss_usdt,
            "risk_sizing_min_quote_usdt": self.cfg.risk_sizing_min_quote_usdt,
            "risk_sizing_max_multiplier": self.cfg.risk_sizing_max_multiplier,
            "onchain_zero_amount_cooldown_sec": self.cfg.onchain_zero_amount_cooldown_sec,
            "onchain_min_hold_seconds": self.cfg.onchain_min_hold_seconds,
            "onchain_max_hold_minutes": self.cfg.onchain_max_hold_minutes,
            "onchain_take_profit_pct": self.cfg.onchain_take_profit_pct,
            "onchain_stop_loss_pct": self.cfg.onchain_stop_loss_pct,
            "onchain_trailing_stop_pct": self.cfg.onchain_trailing_stop_pct,
            "onchain_panic_drop_5m_pct": self.cfg.onchain_panic_drop_5m_pct,
            "onchain_panic_drop_1h_pct": self.cfg.onchain_panic_drop_1h_pct,
            "onchain_tp_partial_ratio": self.cfg.onchain_tp_partial_ratio,
            "onchain_tp_second_multiplier": self.cfg.onchain_tp_second_multiplier,
            "onchain_stagnation_min_hold_minutes": self.cfg.onchain_stagnation_min_hold_minutes,
            "onchain_stagnation_low_liq_hold_minutes": self.cfg.onchain_stagnation_low_liq_hold_minutes,
            "onchain_stagnation_liq_threshold": self.cfg.onchain_stagnation_liq_threshold,
            "onchain_stagnation_holder_threshold": self.cfg.onchain_stagnation_holder_threshold,
            "onchain_stagnation_max_volume_1h_usdt": self.cfg.onchain_stagnation_max_volume_1h_usdt,
            "onchain_stagnation_max_volume_5m_usdt": self.cfg.onchain_stagnation_max_volume_5m_usdt,
            "onchain_stagnation_max_abs_change_1h_pct": self.cfg.onchain_stagnation_max_abs_change_1h_pct,
            "onchain_stagnation_max_abs_change_5m_pct": self.cfg.onchain_stagnation_max_abs_change_5m_pct,
            "onchain_stagnation_max_pnl_pct": self.cfg.onchain_stagnation_max_pnl_pct,
            "onchain_min_sellback_ratio": self.cfg.onchain_min_sellback_ratio,
        }

        meta["last_ts"] = now_ts
        meta["last_at"] = now_str()
        meta["last_stats"] = context.get("recent_trade_stats") or {}
        meta["last_source_stats"] = dict(self._last_source_stats)
        meta["params"] = params_snapshot
        meta["last_review"] = {
            "decision": str(review_data.get("decision") or ("adjust" if changed else "hold")),
            "confidence": to_int(review_data.get("confidence"), 0),
            "summary": str(review_data.get("summary") or "")[:500],
            "market_regime": str(review_data.get("market_regime") or ""),
            "changed": {k: {"old": v[0], "new": v[1]} for k, v in changed.items()},
            "error": error_text,
        }
        self.state["auto_evolve"] = meta

        append_jsonl(EVOLVE_REVIEW_FILE, {
            "ts": now_str(),
            "mode": self.cfg.mode,
            "model": "openclaw-main",
            "decision": meta["last_review"].get("decision"),
            "confidence": meta["last_review"].get("confidence"),
            "summary": meta["last_review"].get("summary"),
            "market_regime": meta["last_review"].get("market_regime"),
            "changed": meta["last_review"].get("changed"),
            "error": error_text,
            "context": context,
            "response_text": response_text,
        })
        self._send_evolve_report(review_data, changed, error_text)

        if changed:
            summary = ", ".join(f"{k}:{v[0]}->{v[1]}" for k, v in changed.items())
            log(f"[EVOLVE-LLM] 8小时复盘完成，参数已调整: {summary}")
        elif not error_text:
            log("[EVOLVE-LLM] 8小时复盘完成，本轮无参数调整")

    def _save_state(self) -> None:
        state_file = current_state_file(self.cfg.dry_run)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = state_file.with_suffix(state_file.suffix + ".tmp")
        tmp_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_file.replace(state_file)

    def _reset_daily_budget_if_needed(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["spent"] = 0.0
            self.state["loss"] = 0.0
            self._save_state()

    def _zero_amount_block_left(self, contract_address: str) -> int:
        key = str(contract_address or "").strip().lower()
        if not key:
            return 0
        blocked = self.state.get("zero_amount_until") or {}
        until = to_int(blocked.get(key), 0)
        now_ts = int(time.time())
        if until <= now_ts:
            if key in blocked:
                blocked.pop(key, None)
                self.state["zero_amount_until"] = blocked
            hits = self.state.get("zero_amount_hits") or {}
            if key in hits:
                hits.pop(key, None)
                self.state["zero_amount_hits"] = hits
            return 0
        return until - now_ts

    def _mark_zero_amount_block(self, contract_address: str) -> int:
        key = str(contract_address or "").strip().lower()
        if not key:
            return 0

        hits = self.state.get("zero_amount_hits") or {}
        hit_count = max(0, to_int(hits.get(key), 0)) + 1
        hits[key] = hit_count
        self.state["zero_amount_hits"] = hits

        base_cooldown = max(60, int(self.cfg.onchain_zero_amount_cooldown_sec))
        first_cooldown = max(60, min(base_cooldown, 180))
        second_cooldown = max(first_cooldown, min(base_cooldown, 360))
        if hit_count <= 1:
            cooldown = first_cooldown
        elif hit_count == 2:
            cooldown = second_cooldown
        else:
            cooldown = base_cooldown

        until = int(time.time()) + cooldown
        blocked = self.state.get("zero_amount_until") or {}
        blocked[key] = until
        self.state["zero_amount_until"] = blocked
        return cooldown

    def _mark_transient_error_block(self, contract_address: str, cooldown_sec: int | None = None) -> int:
        key = str(contract_address or "").strip().lower()
        if not key:
            return 0
        cooldown = max(60, to_int(cooldown_sec, self._onchain_timeout_cooldown_sec))
        until = int(time.time()) + cooldown
        blocked = self.state.get("zero_amount_until") or {}
        blocked[key] = until
        self.state["zero_amount_until"] = blocked
        return cooldown

    @staticmethod
    def _looks_like_timeout_error(msg: str) -> bool:
        text = str(msg or "").strip().lower()
        if not text:
            return False
        timeout_tokens = [
            "read timed out",
            "timed out",
            "timeout",
            "connecttimeout",
            "readtimeout",
            "max retries exceeded",
            "httpsconnectionpool",
            "eof occurred",
        ]
        return any(tok in text for tok in timeout_tokens)

    def _adopt_onchain_position_from_balance(self, token: str, contract: str, quote: float, score: float, detail: dict[str, Any], source: str = "timeout_probe") -> bool:
        if not self.onchain_trader.ensure_ready():
            return False
        contract_text = str(contract or "").strip()
        if not ADDR_RE.fullmatch(contract_text):
            return False

        pos_key = f"ONCHAIN:{contract_text.lower()}"
        positions = self.state.setdefault("positions", {})
        if pos_key in positions:
            return True

        try:
            bal_raw = self.onchain_trader.token_balance_raw(contract_text)
        except Exception:
            return False
        if bal_raw <= 0:
            return False

        est_quote_usdt = 0.0
        try:
            bnb_usdt = self._ticker_price("BNBUSDT")
        except Exception:
            bnb_usdt = 0.0
        if bnb_usdt > 0:
            try:
                funds_wei = self.onchain_trader.estimate_sell_funds_wei(contract_text, bal_raw)
                est_quote_usdt = float(Web3.from_wei(int(funds_wei), "ether")) * bnb_usdt
            except Exception:
                est_quote_usdt = 0.0

        entry_quote_usdt = max(0.01, to_float(quote, 0.0))
        if est_quote_usdt > 0:
            entry_quote_usdt = max(entry_quote_usdt, est_quote_usdt)

        opened_at = int(time.time())
        position_id = f"onchain-{opened_at}-{contract_text.lower()[-6:]}-adopt"
        positions[pos_key] = {
            "route": "onchain",
            "entry_price": 0.0,
            "entry_quote_usdt": entry_quote_usdt,
            "peak_quote_usdt": max(entry_quote_usdt, est_quote_usdt),
            "qty": 0.0,
            "qty_raw": str(int(bal_raw)),
            "opened_at": opened_at,
            "position_id": position_id,
            "score": to_float(score, 0.0),
            "token": str(token or contract_text),
            "contract": contract_text,
            "riskLevel": to_int(detail.get("riskLevel"), 3),
            "liquidity": to_float(detail.get("liquidity"), 0.0),
            "holders": to_int(detail.get("holders"), 0),
            "tp1_done": False,
            "adopted": True,
            "adopt_source": source,
        }
        return True

    def _extract_recent_onchain_buys(self) -> list[dict[str, Any]]:
        log_file = current_log_file()
        if not log_file.exists():
            return []

        try:
            with log_file.open("r", encoding="utf-8", errors="ignore") as f:
                lines = list(deque(f, maxlen=int(self._orphan_reconcile_scan_lines)))
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        now_ts = int(time.time())
        for raw in reversed(lines):
            if "[BUY-ONCHAIN]" not in raw:
                continue

            m_contract = re.search(r"contract=(0x[a-fA-F0-9]{40})", raw)
            if not m_contract:
                continue

            contract = m_contract.group(1)
            key = contract.lower()
            if key in seen:
                continue

            seen.add(key)

            token = contract
            m_token = re.search(r"token=(.*?)\s+score=", raw)
            if m_token:
                token = str(m_token.group(1) or "").strip() or token

            entry_quote = 0.0
            m_quote = re.search(r"\squote=([0-9]+(?:\.[0-9]+)?)", raw)
            if m_quote:
                entry_quote = max(0.0, to_float(m_quote.group(1), 0.0))

            pos_id = ""
            m_pos_id = re.search(r"\bpos_id=([^\s]+)", raw)
            if m_pos_id:
                pos_id = str(m_pos_id.group(1) or "").strip()

            opened_at = now_ts
            m_ts = re.search(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", raw)
            if m_ts:
                try:
                    opened_at = int(datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
                except Exception:
                    opened_at = now_ts

            out.append(
                {
                    "token": token,
                    "contract": contract,
                    "entry_quote_usdt": entry_quote,
                    "opened_at": opened_at,
                    "position_id": pos_id,
                    "source": "buy_log",
                }
            )
            if len(out) >= int(self._orphan_reconcile_max_items):
                break

        return out

    def _reconcile_onchain_orphan_positions(self) -> None:
        if not self.cfg.onchain_sell_enabled or not self.onchain_trader.ensure_ready():
            return

        now_ts = int(time.time())
        if now_ts - int(self._orphan_reconcile_ts) < int(self._orphan_reconcile_interval_sec):
            return
        self._orphan_reconcile_ts = now_ts

        candidates = self._extract_recent_onchain_buys()
        if not candidates:
            return

        try:
            bnb_usdt = self._ticker_price("BNBUSDT")
        except Exception:
            return
        if bnb_usdt <= 0:
            return

        positions = self.state.get("positions") or {}
        adopted = 0
        for item in candidates:
            contract = str(item.get("contract") or "").strip()
            if not ADDR_RE.fullmatch(contract):
                continue

            pos_key = f"ONCHAIN:{contract.lower()}"
            if pos_key in positions:
                continue

            try:
                bal_raw = self.onchain_trader.token_balance_raw(contract)
            except Exception:
                continue
            if bal_raw <= 0:
                continue

            try:
                funds_wei = self.onchain_trader.estimate_sell_funds_wei(contract, bal_raw)
            except Exception:
                continue

            est_quote_usdt = float(Web3.from_wei(int(funds_wei), "ether")) * bnb_usdt
            if est_quote_usdt < float(self._orphan_reconcile_min_usdt):
                continue

            entry_quote_usdt = max(0.01, to_float(item.get("entry_quote_usdt"), 0.0))
            if entry_quote_usdt <= 0.01:
                entry_quote_usdt = max(0.01, est_quote_usdt)

            token_name = str(item.get("token") or contract)
            opened_at = to_int(item.get("opened_at"), now_ts)
            position_id = str(item.get("position_id") or "").strip() or f"onchain-{opened_at}-{contract.lower()[-6:]}-recon"
            positions[pos_key] = {
                "route": "onchain",
                "entry_price": 0.0,
                "entry_quote_usdt": entry_quote_usdt,
                "peak_quote_usdt": max(entry_quote_usdt, est_quote_usdt),
                "qty": 0.0,
                "opened_at": opened_at,
                "position_id": position_id,
                "score": 0.0,
                "token": token_name,
                "contract": contract,
                "riskLevel": 3,
                "liquidity": 0.0,
                "holders": 0,
                "tp1_done": False,
                "adopted": True,
                "adopt_source": str(item.get("source") or "unknown"),
            }
            adopted += 1
            log(
                f"[RECONCILE-ONCHAIN] pos_id={position_id} adopted token={token_name} contract={contract} "
                f"entry_quote={entry_quote_usdt:.4f} est_quote={est_quote_usdt:.4f} bal_raw={bal_raw} source={item.get('source')}"
            )

        if adopted > 0:
            self.state["positions"] = positions

    def _load_exchange_symbols(self) -> dict[str, dict[str, Any]]:
        info = self.spot.public_get("/api/v3/exchangeInfo")
        out: dict[str, dict[str, Any]] = {}
        for item in (info or {}).get("symbols", []):
            if item.get("status") != "TRADING":
                continue
            sym = str(item.get("symbol") or "")
            out[sym] = item
        return out

    def _candidate_key(self, item: dict[str, Any]) -> str:
        chain_id = str(item.get("chainId") or self.cfg.signal_chain_id).strip()
        contract = str(item.get("contractAddress") or "").strip().lower()
        symbol = str(item.get("symbol") or "").strip().upper()
        if contract:
            return f"ca:{chain_id}:{contract}"
        return f"sym:{chain_id}:{symbol}"

    def _candidate_label(self, item: dict[str, Any]) -> str:
        symbol = str(item.get("symbol") or "?")
        contract = str(item.get("contractAddress") or "").strip()
        if contract and len(contract) > 12:
            return f"{symbol}@{contract[:8]}...{contract[-4:]}"
        if contract:
            return f"{symbol}@{contract}"
        return symbol

    def _extract_address_position_token(self, pos: dict[str, Any], default_chain_id: str) -> tuple[str, str, str]:
        symbol = ""
        for key in ("symbol", "tokenSymbol", "coinSymbol", "baseSymbol", "ticker"):
            value = str((pos or {}).get(key) or "").strip()
            if value:
                symbol = value
                break

        contract = ""
        for key in ("contractAddress", "tokenAddress", "coinAddress", "address"):
            value = str((pos or {}).get(key) or "").strip()
            if ADDR_RE.fullmatch(value):
                contract = value
                break

        chain_id = str((pos or {}).get("chainId") or default_chain_id).strip() or default_chain_id
        return symbol, contract, chain_id

    def _auto_follow_wallet_addresses(self) -> list[str]:
        if not self.cfg.smart_wallet_auto_collect:
            return []

        now_ts = int(time.time())
        if self._smart_wallet_cache_addresses and now_ts - self._smart_wallet_cache_ts < int(self.cfg.smart_wallet_cache_sec):
            return self._smart_wallet_cache_addresses

        addresses: list[str] = []
        try:
            leaderboard = self.skills.pnl_leaderboard(
                chain_id=self.cfg.signal_chain_id,
                page_no=1,
                page_size=max(25, int(self.cfg.smart_wallet_auto_limit)),
                tag="ALL",
            )
            for item in leaderboard:
                addr = str((item or {}).get("address") or "").strip()
                if not ADDR_RE.fullmatch(addr):
                    continue
                try:
                    addresses.append(Web3.to_checksum_address(addr))
                except Exception:
                    continue
        except Exception as e:
            log(f"[WARN] 自动收集聪明钱包失败: {str(e)[:120]}")

        dedup: list[str] = []
        seen: set[str] = set()
        for addr in addresses:
            low = addr.lower()
            if low in seen:
                continue
            seen.add(low)
            dedup.append(addr)
            if len(dedup) >= int(self.cfg.smart_wallet_auto_limit):
                break

        self._smart_wallet_cache_addresses = dedup
        self._smart_wallet_cache_ts = now_ts
        return dedup

    def _hot_square_symbols(self) -> list[str]:
        if not self.cfg.square_news_enabled:
            return []

        now_ts = int(time.time())
        if self._square_news_cache_symbols and now_ts - self._square_news_cache_ts < int(self.cfg.square_news_cache_sec):
            return self._square_news_cache_symbols

        if not self.square.can_read():
            self._square_news_cache_symbols = []
            self._square_news_cache_ts = now_ts
            if not self._square_news_warned:
                self._square_news_warned = True
                log("[WARN] Binance Square 新闻源未配置可读 cookie/session，当前仅有发帖 key 不能读取 feed")
            return []

        symbols: list[str] = []
        try:
            items = self.square.fetch_items(limit=int(self.cfg.square_news_limit))
            cutoff_ms = (now_ts - int(self.cfg.square_news_max_age_sec)) * 1000
            for item in items:
                item_ts = _square_item_timestamp_ms(item)
                if item_ts > 0 and item_ts < cutoff_ms:
                    continue
                symbols.extend(_square_item_symbols(item))
        except Exception as e:
            msg = str(e)[:180]
            if "square_feed_blocked_403" in msg:
                log("[WARN] Binance Square 新闻源被 403/WAF 拦截，需更新 cookie/session")
            else:
                if _is_timeout_error(e):
                    send_timeout_telegram_alert("binance_square_feed", str(e))
                log(f"[WARN] Binance Square 获取热点失败: {msg}")

        dedup: list[str] = []
        seen: set[str] = set()
        for sym in symbols:
            norm = _normalize_symbol(sym)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            dedup.append(norm)
            if len(dedup) >= int(self.cfg.square_news_limit):
                break

        self._square_news_cache_symbols = dedup
        self._square_news_cache_ts = now_ts
        return dedup

    def _hot_news_symbols(self) -> list[str]:
        if not self.cfg.news_enabled and not self.cfg.square_news_enabled:
            return []

        now_ts = int(time.time())
        if self._news_cache_symbols and now_ts - self._news_cache_ts < int(self.cfg.news_cache_sec):
            return self._news_cache_symbols

        symbols: list[str] = []
        if self.cfg.news_enabled:
            opennews_token = str(os.getenv("OPENNEWS_TOKEN") or "").strip()
            if not opennews_token:
                log("[WARN] OPENNEWS_TOKEN 未配置，OpenNews 源跳过")
            else:
                try:
                    resp = requests.post(
                        "https://ai.6551.io/open/news_search",
                        headers={
                            "Authorization": f"Bearer {opennews_token}",
                            "Content-Type": "application/json",
                        },
                        json={"limit": int(self.cfg.news_limit), "page": 1, "hasCoin": True},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    data = payload.get("data") if isinstance(payload, dict) else []
                    cutoff_ms = (now_ts - int(self.cfg.news_max_age_sec)) * 1000
                    if isinstance(data, list):
                        for item in data:
                            article_ts = to_int((item or {}).get("ts"), 0)
                            if article_ts <= 0 or article_ts < cutoff_ms:
                                continue
                            for coin in (item or {}).get("coins") or []:
                                sym = str((coin or {}).get("symbol") or "").upper().strip()
                                if sym and len(sym) <= 16:
                                    symbols.append(sym)
                except Exception as e:
                    if _is_timeout_error(e):
                        send_timeout_telegram_alert("opennews_search", str(e))
                    log(f"[WARN] opennews 获取热点失败: {str(e)[:120]}")

        if self.cfg.square_news_enabled:
            symbols.extend(self._hot_square_symbols())

        dedup: list[str] = []
        seen: set[str] = set()
        limit_cap = max(int(self.cfg.news_limit), int(self.cfg.square_news_limit), 5)
        for sym in symbols:
            norm = _normalize_symbol(sym)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            dedup.append(norm)
            if len(dedup) >= limit_cap:
                break

        self._news_cache_symbols = dedup
        self._news_cache_ts = now_ts
        return dedup

    def _candidate_symbols(self) -> list[dict[str, Any]]:
        chain_id = self.cfg.signal_chain_id
        signals = self.skills.smart_money_signals(chain_id=chain_id, page_size=100)

        ranks: list[dict[str, Any]] = []
        alpha: list[dict[str, Any]] = []
        for page in range(1, int(self.cfg.rank_pages) + 1):
            try:
                ranks.extend(self.skills.unified_rank(chain_id=chain_id, rank_type=10, size=self.cfg.rank_page_size, page=page))
            except Exception as e:
                log(f"[WARN] unified_rank rankType=10 page={page} 失败: {str(e)[:120]}")
            try:
                alpha.extend(self.skills.unified_rank(chain_id=chain_id, rank_type=20, size=self.cfg.rank_page_size, page=page))
            except Exception as e:
                log(f"[WARN] unified_rank rankType=20 page={page} 失败: {str(e)[:120]}")

        meme = self.skills.meme_rush(chain_id=chain_id, rank_type=20, limit=120)
        topics = self.skills.topic_rush(chain_id=chain_id, rank_type=20)

        by_key: dict[str, dict[str, Any]] = {}

        def is_noisy_topic_symbol(sym: str) -> bool:
            s = str(sym or "").strip().upper()
            if not s:
                return True
            if len(s) < 2 or len(s) > 12:
                return True
            topic_block = {
                "INSTREET", "CLAWDCHAT", "BNBAI", "OPENCLAW", "CHAT", "AI", "NEWS",
                "小龙虾", "虾聊", "虾说周刊"
            }
            if s in topic_block:
                return True
            # 纯中文/泛主题词/明显非标准 ticker 的 topic 候选，先视为噪声
            if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", s):
                return True
            if not re.fullmatch(r"[A-Z][A-Z0-9\u4e00-\u9fff]{1,11}", s):
                return True
            return False

        def ensure(symbol: str, contract: str = "", chain: str = chain_id) -> dict[str, Any]:
            s = str(symbol or "").upper().strip()
            c = str(contract or "").strip()
            ch = str(chain or chain_id).strip()
            if not s and not c:
                return {}

            key = f"ca:{ch}:{c.lower()}" if c else f"sym:{ch}:{s}"
            if key not in by_key:
                by_key[key] = {
                    "symbol": s or "UNKNOWN",
                    "contractAddress": c,
                    "chainId": ch,
                    "signal_count": 0,
                    "smart_money_count": 0,
                    "in_rank": False,
                    "in_alpha": False,
                    "in_meme": False,
                    "in_topic": False,
                    "topic_net_inflow": 0.0,
                    "in_follow_wallet": False,
                    "follow_wallet_count": 0,
                    "in_news": False,
                    "news_count": 0,
                    "topic_noisy": False,
                }
            else:
                # 逐步补全 symbol/contract
                if c and not by_key[key].get("contractAddress"):
                    by_key[key]["contractAddress"] = c
                if s and (not by_key[key].get("symbol") or by_key[key].get("symbol") == "UNKNOWN"):
                    by_key[key]["symbol"] = s
            if is_noisy_topic_symbol(s):
                by_key[key]["topic_noisy"] = True
            return by_key[key]

        for r in signals:
            symbol = str(r.get("ticker") or r.get("symbol") or "")
            contract = str(r.get("contractAddress") or "")
            ch = str(r.get("chainId") or chain_id)
            c = ensure(symbol, contract, ch)
            if not c:
                continue
            c["signal_count"] += 1
            c["smart_money_count"] = max(c["smart_money_count"], to_int(r.get("smartMoneyCount"), 0))

        for r in ranks:
            c = ensure(str(r.get("symbol") or ""), str(r.get("contractAddress") or ""), str(r.get("chainId") or chain_id))
            if c:
                c["in_rank"] = True

        for r in alpha:
            c = ensure(str(r.get("symbol") or ""), str(r.get("contractAddress") or ""), str(r.get("chainId") or chain_id))
            if c:
                c["in_alpha"] = True

        for r in meme:
            c = ensure(str(r.get("symbol") or ""), str(r.get("contractAddress") or ""), str(r.get("chainId") or chain_id))
            if c:
                c["in_meme"] = True

        for topic in topics:
            inflow = to_float(topic.get("topicNetInflow"), 0.0)
            topic_chain = str(topic.get("chainId") or chain_id)
            for tk in topic.get("tokenList") or []:
                symbol = str(tk.get("symbol") or "")
                contract = str(tk.get("contractAddress") or "")
                c = ensure(symbol, contract, str(tk.get("chainId") or topic_chain))
                if not c:
                    continue
                # topic-rush 噪声抑制：只有 topic、没有合约、且属于明显泛词/噪声时，直接跳过
                if c.get("topic_noisy") and not contract:
                    continue
                c["in_topic"] = True
                c["topic_net_inflow"] = max(c["topic_net_inflow"], inflow)

        manual_wallets = [addr for addr in self.cfg.follow_wallet_addresses]
        auto_wallets = self._auto_follow_wallet_addresses()
        follow_wallets: list[str] = []
        seen_wallet: set[str] = set()
        for addr in manual_wallets + auto_wallets:
            low = str(addr or "").lower()
            if not low or low in seen_wallet:
                continue
            seen_wallet.add(low)
            follow_wallets.append(addr)

        for wallet_addr in follow_wallets:
            try:
                positions = self.skills.address_positions(chain_id, wallet_addr)
            except Exception as e:
                log(f"[WARN] smart_wallet 地址查询失败 {wallet_addr[:8]}...: {str(e)[:100]}")
                continue

            picked = 0
            for pos in positions:
                symbol, contract, pos_chain = self._extract_address_position_token(pos, chain_id)
                c = ensure(symbol, contract, pos_chain)
                if not c:
                    continue
                c["in_follow_wallet"] = True
                c["follow_wallet_count"] = to_int(c.get("follow_wallet_count"), 0) + 1
                picked += 1
                if picked >= int(self.cfg.follow_wallet_top_n):
                    break

        # 如果地址持仓接口没有返回可用 token，回退使用 PnL 排行的 topEarningTokens
        try:
            leaderboard = self.skills.pnl_leaderboard(
                chain_id=chain_id,
                page_no=1,
                page_size=max(25, int(self.cfg.smart_wallet_auto_limit)),
                tag="ALL",
            )
            for row in leaderboard[: int(self.cfg.smart_wallet_auto_limit)]:
                for tk in (row or {}).get("topEarningTokens") or []:
                    symbol = str((tk or {}).get("tokenSymbol") or "").strip()
                    contract = str((tk or {}).get("tokenAddress") or "").strip()
                    c = ensure(symbol, contract, chain_id)
                    if not c:
                        continue
                    c["in_follow_wallet"] = True
                    c["follow_wallet_count"] = to_int(c.get("follow_wallet_count"), 0) + 1
        except Exception as e:
            log(f"[WARN] smart_wallet topEarningTokens 获取失败: {str(e)[:100]}")

        news_symbols = self._hot_news_symbols()
        for sym in news_symbols:
            c = ensure(sym, "", chain_id)
            if not c:
                continue
            c["in_news"] = True
            c["news_count"] = to_int(c.get("news_count"), 0) + 1

        candidates = list(by_key.values())
        candidates.sort(
            key=lambda x: (
                x["in_alpha"],
                x["in_meme"],
                x["in_rank"],
                x["signal_count"],
                x["smart_money_count"],
                x["in_topic"],
                x["topic_net_inflow"],
                x["follow_wallet_count"],
                x["in_news"],
                not x.get("topic_noisy", False),
            ),
            reverse=True,
        )
        follow_count = sum(1 for x in candidates if to_int(x.get("follow_wallet_count"), 0) > 0)
        news_count = sum(1 for x in candidates if bool(x.get("in_news")))
        square_news_count = len(self._square_news_cache_symbols) if self.cfg.square_news_enabled else 0
        self._last_source_stats = {
            "candidates": len(candidates),
            "news_tokens": news_count,
            "square_news_tokens": square_news_count,
            "follow_tokens": follow_count,
            "signals": len(signals),
            "ranks": len(ranks),
            "alpha": len(alpha),
            "meme": len(meme),
            "topics": len(topics),
        }
        log(f"[SOURCE] smart_signals={len(signals)} rank={len(ranks)} alpha={len(alpha)} meme={len(meme)} topics={len(topics)} follow_wallet_tokens={follow_count} follow_wallet_addrs={len(follow_wallets)} news_fresh_tokens={news_count} square_news_tokens={square_news_count} news_max_age={self.cfg.news_max_age_sec}s square_news_max_age={self.cfg.square_news_max_age_sec}s dedup={len(candidates)}")

        if self.cfg.max_candidates_per_loop and self.cfg.max_candidates_per_loop > 0:
            return candidates[: self.cfg.max_candidates_per_loop]
        return candidates

    def _map_to_binance_symbol(self, token_symbol: str) -> str | None:
        pair = f"{token_symbol.upper()}{self.cfg.quote_asset}"
        if pair in self.exchange_map:
            return pair
        return None

    def _symbol_info(self, symbol: str) -> dict[str, Any]:
        info = self.exchange_map.get(str(symbol or "").upper())
        return info if isinstance(info, dict) else {}

    def _symbol_filter(self, symbol: str, filter_type: str) -> dict[str, Any]:
        info = self._symbol_info(symbol)
        for item in info.get("filters") or []:
            if isinstance(item, dict) and str(item.get("filterType") or "") == filter_type:
                return item
        return {}

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return max(0.0, float(value))
        q = (Decimal(str(max(0.0, value))) / Decimal(str(step))).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return float(q * Decimal(str(step)))

    def _normalize_spot_sell_qty(self, symbol: str, qty: float) -> tuple[float, str]:
        qty = max(0.0, float(qty))
        market_lot = self._symbol_filter(symbol, "MARKET_LOT_SIZE")
        lot = self._symbol_filter(symbol, "LOT_SIZE")
        f = market_lot or lot
        min_qty = to_float(f.get("minQty"), 0.0)
        step_size = to_float(f.get("stepSize"), 0.0)
        max_qty = to_float(f.get("maxQty"), 0.0)

        norm = self._floor_to_step(qty, step_size) if step_size > 0 else qty
        if max_qty > 0:
            norm = min(norm, max_qty)
        if min_qty > 0 and norm < min_qty:
            return 0.0, f"qty<{min_qty}"
        return norm, "ok"

    @staticmethod
    def _extract_spot_buy_fill(resp: dict[str, Any], fallback_price: float, fallback_qty: float, fallback_quote: float) -> tuple[float, float, float]:
        if not isinstance(resp, dict) or bool(resp.get("dryRun")):
            return fallback_price, fallback_qty, fallback_quote
        executed_qty = max(0.0, to_float(resp.get("executedQty"), 0.0))
        quote_qty = max(0.0, to_float(resp.get("cummulativeQuoteQty"), 0.0))
        fills = resp.get("fills") or []
        if isinstance(fills, list) and fills:
            fill_qty = 0.0
            fill_quote = 0.0
            for fill in fills:
                if not isinstance(fill, dict):
                    continue
                q = max(0.0, to_float(fill.get("qty"), 0.0))
                p = max(0.0, to_float(fill.get("price"), 0.0))
                fill_qty += q
                fill_quote += q * p
            if fill_qty > 0:
                executed_qty = fill_qty
                quote_qty = max(quote_qty, fill_quote)
        if executed_qty <= 0:
            executed_qty = max(0.0, fallback_qty)
        if quote_qty <= 0:
            quote_qty = max(0.0, fallback_quote)
        avg_price = fallback_price
        if executed_qty > 0 and quote_qty > 0:
            avg_price = quote_qty / executed_qty
        return avg_price, executed_qty, quote_qty

    def _score_candidate(self, candidate: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        symbol = str(candidate.get("symbol") or "")
        score = 0.0

        # 核心目标：优先使用 Binance Skills 自带的热点/信号能力做链上土狗筛选
        # 但对“只有 topic 没有辅助信号”的候选做噪声抑制
        score += min(28.0, to_int(candidate.get("signal_count"), 0) * 5.5)
        score += min(18.0, to_int(candidate.get("smart_money_count"), 0) * 3.0)
        if candidate.get("in_rank"):
            score += 12
        if candidate.get("in_alpha"):
            score += 14
        if candidate.get("in_meme"):
            score += 12
        if candidate.get("in_topic"):
            score += 10
        score += min(12.0, to_float(candidate.get("topic_net_inflow"), 0.0) / 4000.0)

        # 地址信息仍保留，但不再压过 Binance 自带热点能力
        follow_wallet_count = to_int(candidate.get("follow_wallet_count"), 0)
        if candidate.get("in_follow_wallet"):
            score += min(10.0, follow_wallet_count * 2.5)

        # 外部新闻源降权；若配置关闭则自然不会参与
        news_count = to_int(candidate.get("news_count"), 0)
        if candidate.get("in_news"):
            score += min(4.0, max(1, news_count) * 1.5)

        topic_only = bool(candidate.get("in_topic")) and not any([
            bool(candidate.get("in_alpha")),
            bool(candidate.get("in_meme")),
            bool(candidate.get("in_rank")),
            to_int(candidate.get("signal_count"), 0) > 0,
            to_int(candidate.get("smart_money_count"), 0) > 0,
            to_int(candidate.get("follow_wallet_count"), 0) > 0,
        ])
        if topic_only:
            score -= 10.0
        if candidate.get("topic_noisy"):
            score -= 18.0

        contract = str(candidate.get("contractAddress") or "").strip()
        chain_id = str(candidate.get("chainId") or self.cfg.signal_chain_id).strip()

        detail: dict[str, Any] = {
            "base_score": round(score, 2),
            "contract": contract,
            "chainId": chain_id,
            "follow_wallet_count": follow_wallet_count,
            "news_count": news_count,
        }

        name_block_reason = self._token_name_hard_block_reason(symbol)
        if name_block_reason:
            detail["hard_block_reason"] = name_block_reason
            score -= 100.0

        # 若候选里没有合约，才按 symbol 回补（尽量避免同名误指向）
        if not contract:
            try:
                search = self.skills.token_search(symbol, chain_ids=self.cfg.signal_chain_id)
                if search:
                    token = search[0]
                    contract = str(token.get("contractAddress") or "").strip()
                    chain_id = str(token.get("chainId") or chain_id).strip()
                    detail["contract"] = contract
                    detail["chainId"] = chain_id
            except Exception as e:
                detail["symbol_search_error"] = str(e)[:120]

        # query-token-info + query-token-audit
        if contract:
            try:
                dynamic = self.skills.token_dynamic(chain_id, contract)
                liquidity = to_float(dynamic.get("liquidity"), 0.0)
                holders = to_int(dynamic.get("holders"), 0)
                if liquidity >= 100000:
                    score += 8
                elif liquidity >= 30000:
                    score += 4
                if holders >= 5000:
                    score += 5
                elif holders >= 1000:
                    score += 2
                detail["liquidity"] = liquidity
                detail["holders"] = holders

                if liquidity < float(self.cfg.onchain_min_entry_liquidity_usdt):
                    detail["hard_block_reason"] = f"entry_liquidity_too_low:{liquidity:.2f}"
                    score -= 120.0
                elif liquidity > float(self.cfg.onchain_max_entry_liquidity_usdt):
                    detail["hard_block_reason"] = f"entry_liquidity_too_high:{liquidity:.2f}"
                    score -= 120.0

                if holders < int(self.cfg.onchain_min_entry_holders):
                    detail["hard_block_reason"] = detail.get("hard_block_reason") or f"entry_holders_too_low:{holders}"
                    score -= 120.0
                elif holders > int(self.cfg.onchain_max_entry_holders):
                    detail["hard_block_reason"] = detail.get("hard_block_reason") or f"entry_holders_too_high:{holders}"
                    score -= 120.0

                launch_time_ms = to_int(dynamic.get("launchTime"), 0)
                detail["launchTime"] = launch_time_ms
                if launch_time_ms > 0:
                    age_minutes = max(0.0, (time.time() * 1000.0 - float(launch_time_ms)) / 60000.0)
                    detail["launch_age_minutes"] = round(age_minutes, 2)
                    if age_minutes < float(self.cfg.onchain_min_launch_age_minutes):
                        detail["hard_block_reason"] = f"launch_age_too_new:{age_minutes:.2f}m"
                        score -= 120.0
                    elif age_minutes > float(self.cfg.onchain_max_launch_age_minutes):
                        detail["hard_block_reason"] = f"launch_age_too_old:{age_minutes:.2f}m"
                        score -= 120.0
                    else:
                        score += 10.0
                else:
                    detail["launch_age_minutes"] = None
                    detail["hard_block_reason"] = detail.get("hard_block_reason") or "launch_age_unknown"
                    score -= 40.0

                audit = self.skills.token_audit(chain_id, contract)
                risk_level = to_int(audit.get("riskLevel"), 3)
                detail["riskLevel"] = risk_level
                if risk_level <= 1:
                    score += 8
                elif risk_level == 2:
                    score += 3
                else:
                    score -= 8

                audit_block_reason = self._extract_audit_hard_block_reason(audit)
                if audit_block_reason:
                    detail["hard_block_reason"] = audit_block_reason
                    score -= 120.0
            except Exception as e:
                detail["skill_error"] = str(e)[:120]
        else:
            detail["missing_contract"] = True

        return round(score, 2), detail

    def _dynamic_min_score(self, candidate: dict[str, Any], detail: dict[str, Any], route: str) -> tuple[float, str]:
        base = float(self.cfg.min_score)
        if not self.cfg.dynamic_min_score_enabled:
            return base, "static"

        threshold = base
        notes: list[str] = []

        liquidity = to_float(detail.get("liquidity"), 0.0)
        holders = to_int(detail.get("holders"), 0)
        risk_level = to_int(detail.get("riskLevel"), 3)
        signal_count = to_int(candidate.get("signal_count"), 0)
        smart_money_count = to_int(candidate.get("smart_money_count"), 0)
        follow_wallet_count = to_int(candidate.get("follow_wallet_count"), 0)
        news_count = to_int(candidate.get("news_count"), 0)

        if route == "onchain":
            if liquidity >= 250000:
                threshold -= 20
                notes.append("liq>=250k:-20")
            elif liquidity >= 120000:
                threshold -= 15
                notes.append("liq>=120k:-15")
            elif liquidity >= 60000:
                threshold -= 12
                notes.append("liq>=60k:-12")
            elif liquidity >= 30000:
                threshold -= 8
                notes.append("liq>=30k:-8")
            elif liquidity >= 15000:
                threshold -= 5
                notes.append("liq>=15k:-5")

            if holders >= 5000:
                threshold -= 5
                notes.append("holders>=5k:-5")
            elif holders >= 2000:
                threshold -= 3
                notes.append("holders>=2k:-3")
            elif holders >= 1000:
                threshold -= 2
                notes.append("holders>=1k:-2")

            if signal_count >= 3:
                threshold -= 3
                notes.append("signal>=3:-3")
            elif signal_count >= 2:
                threshold -= 2
                notes.append("signal>=2:-2")

            if smart_money_count >= 8:
                threshold -= 3
                notes.append("sm>=8:-3")
            elif smart_money_count >= 5:
                threshold -= 2
                notes.append("sm>=5:-2")

            if follow_wallet_count >= 2:
                threshold -= 4
                notes.append("follow>=2:-4")
            elif follow_wallet_count >= 1:
                threshold -= 2
                notes.append("follow>=1:-2")

            if news_count >= 2:
                threshold -= 3
                notes.append("news>=2:-3")
            elif news_count >= 1:
                threshold -= 1
                notes.append("news>=1:-1")

            if risk_level >= 4:
                threshold += 8
                notes.append("risk>=4:+8")
            elif risk_level == 3:
                threshold += 4
                notes.append("risk=3:+4")
            elif risk_level <= 1:
                threshold -= 2
                notes.append("risk<=1:-2")
        else:
            if risk_level >= 4:
                threshold += 5
                notes.append("spot_risk>=4:+5")
            elif risk_level <= 1:
                threshold -= 2
                notes.append("spot_risk<=1:-2")

        floor = max(1.0, float(self.cfg.dynamic_min_score_floor))
        ceiling = base + 12.0
        threshold = max(floor, min(ceiling, threshold))

        return round(threshold, 1), ",".join(notes[:6]) if notes else "dyn:no_adjust"

    def _risk_adjusted_quote(self, base_quote: float, score: float, min_required: float, detail: dict[str, Any], route: str) -> tuple[float, str]:
        if base_quote <= 0:
            return 0.0, "base<=0"
        if not self.cfg.risk_sizing_enabled:
            return base_quote, "risk_sizing=off"

        risk_level = to_int(detail.get("riskLevel"), 3)
        liquidity = to_float(detail.get("liquidity"), 0.0)
        confidence = float(score) - float(min_required)

        multiplier = 1.0
        notes: list[str] = []

        if route == "onchain":
            if risk_level >= 4:
                multiplier *= 0.45
                notes.append("risk>=4:x0.45")
            elif risk_level == 3:
                multiplier *= 0.65
                notes.append("risk=3:x0.65")
            elif risk_level == 2:
                multiplier *= 0.85
                notes.append("risk=2:x0.85")
            else:
                multiplier *= 1.10
                notes.append("risk<=1:x1.10")

            if liquidity >= 250000:
                multiplier *= 1.15
                notes.append("liq>=250k:x1.15")
            elif liquidity >= 120000:
                multiplier *= 1.08
                notes.append("liq>=120k:x1.08")
            elif liquidity < 15000:
                multiplier *= 0.70
                notes.append("liq<15k:x0.70")
        else:
            if risk_level >= 4:
                multiplier *= 0.60
                notes.append("spot_risk>=4:x0.60")
            elif risk_level <= 1:
                multiplier *= 1.05
                notes.append("spot_risk<=1:x1.05")

        if confidence >= 25:
            multiplier *= 1.25
            notes.append("conf>=25:x1.25")
        elif confidence >= 15:
            multiplier *= 1.15
            notes.append("conf>=15:x1.15")
        elif confidence >= 8:
            multiplier *= 1.05
            notes.append("conf>=8:x1.05")
        elif confidence < 3:
            multiplier *= 0.75
            notes.append("conf<3:x0.75")

        cap = base_quote * max(1.0, float(self.cfg.risk_sizing_max_multiplier))
        quote = max(0.0, min(cap, base_quote * multiplier))
        return quote, ",".join(notes[:6]) if notes else "risk:no_adjust"

    def _ticker_price(self, symbol: str) -> float:
        data = self.spot.public_get("/api/v3/ticker/price", {"symbol": symbol})
        return to_float(data.get("price"), 0.0)

    def _place_market_buy(self, symbol: str, quote_usdt: float) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_usdt:.2f}",
            "recvWindow": 5000,
        }
        if self.cfg.dry_run:
            return {"dryRun": True, "symbol": symbol, "quoteOrderQty": params["quoteOrderQty"]}
        return self.spot.signed_request("POST", "/api/v3/order", params)

    def _place_market_sell(self, symbol: str, qty: float) -> dict[str, Any]:
        norm_qty, note = self._normalize_spot_sell_qty(symbol, qty)
        if norm_qty <= 0:
            raise RuntimeError(f"spot_sell_qty_invalid:{symbol}:{note}")
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{norm_qty:.8f}",
            "recvWindow": 5000,
        }
        if self.cfg.dry_run:
            return {"dryRun": True, "symbol": symbol, "quantity": params["quantity"], "normalized": True}
        return self.spot.signed_request("POST", "/api/v3/order", params)

    def _is_high_quality_onchain(
        self,
        risk_level: int,
        liquidity: float,
        holders: int,
        holder_growth_ratio: float,
        liquidity_growth_ratio: float,
        pct_5m: float,
        pct_1h: float,
        volume_5m: float,
        volume_1h: float,
    ) -> tuple[bool, str]:
        score = 0
        notes: list[str] = []

        if risk_level <= 1:
            score += 3
            notes.append("risk<=1")
        elif risk_level == 2:
            score += 1
            notes.append("risk=2")
        else:
            score -= 3
            notes.append("risk>=3")

        if liquidity >= 120000:
            score += 3
            notes.append("liq>=120k")
        elif liquidity >= 60000:
            score += 2
            notes.append("liq>=60k")
        elif liquidity >= 30000:
            score += 1
            notes.append("liq>=30k")

        if holders >= 3000:
            score += 2
            notes.append("holders>=3k")
        elif holders >= 1200:
            score += 1
            notes.append("holders>=1.2k")

        if holder_growth_ratio >= 0.60:
            score += 4
            notes.append("holders+>=60%")
        elif holder_growth_ratio >= 0.30:
            score += 3
            notes.append("holders+>=30%")
        elif holder_growth_ratio >= 0.15:
            score += 1
            notes.append("holders+>=15%")

        if liquidity_growth_ratio >= 0.50:
            score += 3
            notes.append("liq+>=50%")
        elif liquidity_growth_ratio >= 0.25:
            score += 2
            notes.append("liq+>=25%")
        elif liquidity_growth_ratio >= 0.10:
            score += 1
            notes.append("liq+>=10%")

        if pct_1h >= 45:
            score += 3
            notes.append("1h>=45%")
        elif pct_1h >= 20:
            score += 2
            notes.append("1h>=20%")

        if pct_5m >= 10:
            score += 2
            notes.append("5m>=10%")
        elif pct_5m >= 4:
            score += 1
            notes.append("5m>=4%")

        if volume_5m >= 5000:
            score += 1
            notes.append("vol5m>=5k")
        if volume_1h >= 40000:
            score += 1
            notes.append("vol1h>=40k")

        if pct_1h <= -10:
            score -= 3
            notes.append("1h<=-10%")
        elif pct_5m <= -5:
            score -= 2
            notes.append("5m<=-5%")

        high_quality = (
            score >= 8
            and risk_level <= 2
            and (holder_growth_ratio >= 0.15 or liquidity_growth_ratio >= 0.15 or pct_1h >= 20)
        )
        return high_quality, f"score={score};" + ",".join(notes[:8])

    def _manage_positions(self) -> None:
        positions = self.state.get("positions") or {}
        to_remove: list[str] = []

        # 卖出管理分批轮询，避免 OKX 卖出估价在单轮里被大量仓位打爆限流
        items = list(positions.items())
        if not items:
            self.state["positions"] = positions
            return
        batch_size = 3 if self.cfg.dry_run else 6
        cursor = max(0, to_int(self.state.get("position_manage_cursor"), 0))
        if cursor >= len(items):
            cursor = 0
        inspect_items = items[cursor:cursor + batch_size]
        if len(inspect_items) < batch_size and items:
            inspect_items += items[: max(0, batch_size - len(inspect_items))]

        # 去重：避免 total < batch_size 时同一持仓在同一轮被重复处理
        deduped_items: list[tuple[str, dict[str, Any]]] = []
        seen_symbols: set[str] = set()
        for sym, p in inspect_items:
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)
            deduped_items.append((sym, p))
        inspect_items = deduped_items

        next_cursor = (cursor + batch_size) % max(1, len(items))
        self.state["position_manage_cursor"] = next_cursor

        bnb_usdt = 0.0
        try:
            bnb_usdt = self._ticker_price("BNBUSDT")
        except Exception:
            bnb_usdt = 0.0

        now_ts = int(time.time())

        # 防重复：同一轮/短时间内同合约只允许一次 SELL
        sell_guard_sec = max(5, to_int(os.getenv("BINANCE_BOT_ONCHAIN_SELL_GUARD_SEC"), 20))
        seen_onchain_contracts: set[str] = set()
        sell_guard_ts = self.state.get("onchain_sell_guard_ts") or {}
        if not isinstance(sell_guard_ts, dict):
            sell_guard_ts = {}

        log(f"[POS-MANAGE] total={len(items)} inspect={len(inspect_items)} cursor={cursor}->{next_cursor} dry_run={self.cfg.dry_run}")
        for symbol, pos in inspect_items:
            # 同一轮中可能先被全平并移除，后续重复引用直接跳过
            if symbol not in positions:
                continue
            pos = positions.get(symbol) or pos
            if bool((pos or {}).get("dry_run_position", False)) != bool(self.cfg.dry_run):
                continue
            route = str((pos or {}).get("route") or "spot")

            if route == "spot":
                entry = to_float(pos.get("entry_price"), 0.0)
                qty = to_float(pos.get("qty"), 0.0)
                if entry <= 0 or qty <= 0:
                    to_remove.append(symbol)
                    continue

                try:
                    last = self._ticker_price(symbol)
                except Exception as e:
                    log(f"[WARN] 拉价格失败 {symbol}: {str(e)[:100]}")
                    continue

                pnl_pct = (last / entry - 1.0) * 100.0
                if pnl_pct >= self.cfg.take_profit_pct or pnl_pct <= self.cfg.stop_loss_pct:
                    reason = "TP" if pnl_pct >= self.cfg.take_profit_pct else "SL"
                    try:
                        resp = self._place_market_sell(symbol, qty)
                        log(f"[SELL-{reason}] {symbol} qty={qty:.8f} pnl={pnl_pct:.2f}% resp={json.dumps(resp, ensure_ascii=False)[:220]}")

                        realized_pnl = (last - entry) * qty
                        if not self.cfg.dry_run:
                            loss_balance = to_float(self.state.get("loss"), 0.0)
                            if realized_pnl < 0:
                                realized_loss = abs(realized_pnl)
                                self.state["loss"] = loss_balance + realized_loss
                                log(f"[LOSS] route=spot symbol={symbol} add={realized_loss:.4f} net_loss={to_float(self.state.get('loss'), 0.0):.4f}")
                            elif realized_pnl > 0 and loss_balance > 0:
                                recover = min(loss_balance, realized_pnl)
                                self.state["loss"] = max(0.0, loss_balance - recover)
                                log(f"[LOSS-RECOVER] route=spot symbol={symbol} recover={recover:.4f} net_loss={to_float(self.state.get('loss'), 0.0):.4f}")

                        to_remove.append(symbol)
                    except Exception as e:
                        log(f"[ERR] 卖出失败 {symbol}: {str(e)[:120]}")
                continue

            if route != "onchain" or not self.cfg.onchain_sell_enabled:
                continue

            token_name = str((pos or {}).get("token") or symbol)
            contract = str((pos or {}).get("contract") or "").strip()
            if not contract:
                to_remove.append(symbol)
                continue

            position_id = str((pos or {}).get("position_id") or "").strip()
            contract_key = contract.lower()
            if contract_key in seen_onchain_contracts:
                continue
            seen_onchain_contracts.add(contract_key)
            last_sell_ts = int(sell_guard_ts.get(contract_key) or 0)
            if last_sell_ts and (now_ts - last_sell_ts) < sell_guard_sec:
                log(f"[SKIP] token={token_name} contract={contract} route=onchain 命中SELL冷却 {now_ts - last_sell_ts}s<{sell_guard_sec}s")
                continue

            if not position_id:
                seed = f"{contract.lower()}:{to_int((pos or {}).get('opened_at'), 0)}:{token_name}"
                position_id = f"onchain-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"
                pos["position_id"] = position_id
                positions[symbol] = pos

            if not self.onchain_trader.ensure_ready():
                log(f"[WARN] onchain 交易器未就绪，跳过持仓管理 token={token_name} err={self.onchain_trader.last_error or 'unknown'}")
                continue

            if self.cfg.dry_run:
                bal_raw = max(0, int(to_float(pos.get("qty"), 0.0)))
                if bal_raw <= 0:
                    log(f"[DRYRUN-HOLD] token={token_name} contract={contract} 模拟持仓数量为0，移除持仓")
                    to_remove.append(symbol)
                    continue
            else:
                bal_raw = self.onchain_trader.token_balance_raw(contract)
                if bal_raw <= 0:
                    to_remove.append(symbol)
                    continue

            entry_quote_usdt = to_float(pos.get("entry_quote_usdt"), 0.0)
            if entry_quote_usdt <= 0:
                entry_quote_usdt = max(0.0, to_float(pos.get("entry_price"), 0.0) * to_float(pos.get("qty"), 0.0))
            if entry_quote_usdt <= 0:
                entry_quote_usdt = to_float(pos.get("quote_usdt"), 0.0)
            if entry_quote_usdt <= 0 or bnb_usdt <= 0:
                continue

            exec_engine = str(pos.get("exec_engine") or "helper")
            try:
                if exec_engine == "okx":
                    funds_wei = self.onchain_trader.estimate_sell_funds_wei_okx(
                        contract,
                        bal_raw,
                        timeout_sec=self.cfg.okx_quote_probe_timeout_sec,
                        max_price_impact_pct=max(12.0, self.cfg.okx_quote_probe_max_price_impact_pct),
                    )
                else:
                    funds_wei = self.onchain_trader.estimate_sell_funds_wei(contract, bal_raw)
            except Exception as e:
                msg = str(e)[:160]
                log(f"[WARN] onchain 估算卖出失败 token={token_name} engine={exec_engine}: {msg}")
                if 'Rate limited' in msg or 'rate limit' in msg.lower() or '429' in msg:
                    time.sleep(0.8)
                continue

            est_quote_usdt = float(Web3.from_wei(int(funds_wei), "ether")) * bnb_usdt
            if est_quote_usdt <= 0:
                continue

            pnl_pct = (est_quote_usdt / entry_quote_usdt - 1.0) * 100.0
            peak_quote_usdt = max(to_float(pos.get("peak_quote_usdt"), 0.0), entry_quote_usdt, est_quote_usdt)
            pos["peak_quote_usdt"] = peak_quote_usdt
            drawdown_pct = (est_quote_usdt / peak_quote_usdt - 1.0) * 100.0 if peak_quote_usdt > 0 else 0.0

            hold_seconds = max(0, now_ts - to_int(pos.get("opened_at"), now_ts))
            tp1_done = bool(pos.get("tp1_done", False))

            entry_risk_level = to_int(pos.get("riskLevel"), 3)
            entry_liquidity = to_float(pos.get("liquidity"), 0.0)
            entry_holders = to_int(pos.get("holders"), 0)
            risk_level = entry_risk_level
            liquidity = entry_liquidity
            holders = entry_holders
            pct_5m = 0.0
            pct_1h = 0.0
            volume_5m = 0.0
            volume_1h = 0.0

            try:
                dyn = self.skills.token_dynamic(self.cfg.signal_chain_id, contract)
                pct_5m = to_float(dyn.get("percentChange5m"), 0.0)
                pct_1h = to_float(dyn.get("percentChange1h"), 0.0)
                volume_5m = to_float(dyn.get("volume5m"), 0.0)
                volume_1h = to_float(dyn.get("volume1h"), 0.0)
                liquidity = to_float(dyn.get("liquidity"), liquidity)
                holders = max(holders, to_int(dyn.get("holders"), holders))
            except Exception:
                pass

            try:
                audit = self.skills.token_audit(self.cfg.signal_chain_id, contract)
                risk_level = to_int(audit.get("riskLevel"), risk_level)
            except Exception:
                pass

            holder_growth_ratio = 0.0
            if entry_holders > 0:
                holder_growth_ratio = (holders - entry_holders) / float(max(entry_holders, 1))
            elif holders > 0:
                holder_growth_ratio = 1.0

            liquidity_growth_ratio = 0.0
            if entry_liquidity > 0:
                liquidity_growth_ratio = (liquidity - entry_liquidity) / float(max(entry_liquidity, 1.0))
            elif liquidity > 0:
                liquidity_growth_ratio = 1.0

            is_high_quality, hq_note = self._is_high_quality_onchain(
                risk_level=risk_level,
                liquidity=liquidity,
                holders=holders,
                holder_growth_ratio=holder_growth_ratio,
                liquidity_growth_ratio=liquidity_growth_ratio,
                pct_5m=pct_5m,
                pct_1h=pct_1h,
                volume_5m=volume_5m,
                volume_1h=volume_1h,
            )

            tp_pct = float(self.cfg.onchain_take_profit_pct)
            sl_pct = float(self.cfg.onchain_stop_loss_pct)
            trailing_pct = float(self.cfg.onchain_trailing_stop_pct)

            if risk_level >= 4:
                tp_pct -= 10
                sl_pct += 6
                trailing_pct -= 4
            elif risk_level == 3:
                tp_pct -= 6
                sl_pct += 4
                trailing_pct -= 2
            elif risk_level <= 1:
                tp_pct += 8
                sl_pct -= 2
                trailing_pct += 2

            if liquidity >= 120000:
                tp_pct += 6
                trailing_pct += 2
            elif liquidity < 15000:
                tp_pct -= 6
                sl_pct += 3
                trailing_pct -= 3

            if holders >= 2000:
                tp_pct += 4
            elif holders < 500:
                tp_pct -= 5
                sl_pct += 2

            vol = max(abs(pct_5m), abs(pct_1h))
            if vol >= 120:
                tp_pct -= 10
                sl_pct += 6
                trailing_pct -= 6
            elif vol >= 60:
                tp_pct -= 6
                sl_pct += 3
                trailing_pct -= 4
            elif vol >= 30:
                tp_pct -= 3
                trailing_pct -= 2

            if is_high_quality:
                tp_pct = max(tp_pct, 100.0)
                trailing_pct = max(trailing_pct, 12.0)

            tp_pct = max(8.0, min(120.0, tp_pct))
            sl_pct = max(-60.0, min(-3.0, sl_pct))
            trailing_pct = max(4.0, min(45.0, trailing_pct))

            reason = ""
            sell_ratio = 1.0

            if hold_seconds < int(self.cfg.onchain_min_hold_seconds):
                if pnl_pct <= (sl_pct - 8) or pct_5m <= (self.cfg.onchain_panic_drop_5m_pct - 8):
                    reason = "onchain_emergency"
            else:
                if pnl_pct <= sl_pct:
                    reason = "onchain_sl"
                elif pct_5m <= self.cfg.onchain_panic_drop_5m_pct or pct_1h <= self.cfg.onchain_panic_drop_1h_pct:
                    reason = "onchain_panic_drop"
                elif pnl_pct > 0 and drawdown_pct <= -abs(trailing_pct):
                    reason = "onchain_trailing"
                elif self.cfg.onchain_stagnation_sell_enabled:
                    stagnation_hold_min = int(self.cfg.onchain_stagnation_min_hold_minutes)
                    if (
                        liquidity <= float(self.cfg.onchain_stagnation_liq_threshold)
                        or holders <= int(self.cfg.onchain_stagnation_holder_threshold)
                        or risk_level >= 3
                    ):
                        stagnation_hold_min = min(stagnation_hold_min, int(self.cfg.onchain_stagnation_low_liq_hold_minutes))

                    recent_quiet = (
                        abs(pct_5m) <= float(self.cfg.onchain_stagnation_max_abs_change_5m_pct)
                        and volume_5m <= float(self.cfg.onchain_stagnation_max_volume_5m_usdt)
                    )
                    mid_quiet = (
                        abs(pct_1h) <= float(self.cfg.onchain_stagnation_max_abs_change_1h_pct)
                        and volume_1h <= float(self.cfg.onchain_stagnation_max_volume_1h_usdt)
                    )
                    stale_window_passed = hold_seconds >= max(600, (stagnation_hold_min + 10) * 60)
                    stagnant = recent_quiet and (mid_quiet or stale_window_passed)
                    if (
                        hold_seconds >= max(120, stagnation_hold_min * 60)
                        and stagnant
                        and pnl_pct <= float(self.cfg.onchain_stagnation_max_pnl_pct)
                        and not (is_high_quality and pnl_pct > 0)
                    ):
                        reason = "onchain_stagnant"
                elif hold_seconds >= int(self.cfg.onchain_max_hold_minutes) * 60 and pnl_pct < max(5.0, tp_pct * 0.6):
                    reason = "onchain_time_stop"
                elif pnl_pct >= tp_pct and not tp1_done:
                    if is_high_quality:
                        reason = "onchain_double_out"
                        sell_ratio = 0.50
                    else:
                        reason = "onchain_tp1"
                        sell_ratio = float(self.cfg.onchain_tp_partial_ratio)
                elif tp1_done and pnl_pct >= tp_pct * float(self.cfg.onchain_tp_second_multiplier):
                    reason = "onchain_tp2"
                elif tp1_done and pnl_pct > 0 and drawdown_pct <= -max(6.0, trailing_pct * 0.7):
                    reason = "onchain_tp_retrace"

            if not reason:
                continue

            sell_amount = bal_raw if sell_ratio >= 0.999 else max(1, int(bal_raw * sell_ratio))
            sold_ratio = float(sell_amount) / float(max(bal_raw, 1))

            try:
                sell_guard_ts[contract_key] = now_ts
                self.state["onchain_sell_guard_ts"] = sell_guard_ts
                if exec_engine == "okx":
                    try:
                        resp = self.onchain_trader.sell_via_okx(
                            contract,
                            sell_amount,
                            self.cfg.onchain_slippage_bps,
                            self.cfg.dry_run,
                            timeout_sec=self.cfg.okx_quote_probe_timeout_sec,
                            max_price_impact_pct=max(12.0, self.cfg.okx_quote_probe_max_price_impact_pct),
                        )
                    except Exception as okx_sell_e:
                        log(f"[WARN] token={token_name} route=onchain OKX卖出失败，回退helper: {str(okx_sell_e)[:160]}")
                        resp = self.onchain_trader.sell(contract, sell_amount, self.cfg.dry_run)
                        resp["fallbackFrom"] = "okx"
                        resp["fallbackReason"] = str(okx_sell_e)[:160]
                else:
                    resp = self.onchain_trader.sell(contract, sell_amount, self.cfg.dry_run)
                log(
                    f"[SELL-ONCHAIN] pos_id={position_id} token={token_name} contract={contract} reason={reason} pnl={pnl_pct:.2f}% drawdown={drawdown_pct:.2f}% "
                    f"est_quote={est_quote_usdt:.4f} entry_quote={entry_quote_usdt:.4f} ratio={sold_ratio:.2f} engine={exec_engine} dry_run_pos={bool(pos.get('dry_run_position', False))} "
                    f"risk={risk_level} liq={liquidity:.0f} holders={holders} hq={is_high_quality}({hq_note}) chg5m={pct_5m:.2f}% chg1h={pct_1h:.2f}% vol5m={volume_5m:.2f} vol1h={volume_1h:.2f} "
                    f"resp={json.dumps(resp, ensure_ascii=False)[:220]}"
                )

                if reason == "onchain_double_out" and not self.cfg.dry_run:
                    tx_hash = str((resp or {}).get("sellTxHash") or "").strip()
                    tx_line = f"\nTx: https://bscscan.com/tx/{tx_hash}" if tx_hash else ""
                    alert_text = (
                        "✅ 币安自动交易触发翻倍出本\n"
                        f"Token: {token_name}\n"
                        f"合约: {contract}\n"
                        f"卖出比例: {sold_ratio * 100:.1f}%\n"
                        f"PnL: {pnl_pct:.2f}%\n"
                        f"估值: {est_quote_usdt:.4f} USDT\n"
                        f"高质量判定: {hq_note}"
                        f"{tx_line}"
                    )
                    send_telegram_alert(alert_text)

                realized_pnl = (est_quote_usdt * sold_ratio) - (entry_quote_usdt * sold_ratio)
                if not self.cfg.dry_run:
                    loss_balance = to_float(self.state.get("loss"), 0.0)
                    if realized_pnl < 0:
                        add_loss = abs(realized_pnl)
                        self.state["loss"] = loss_balance + add_loss
                        log(f"[LOSS] route=onchain token={token_name} add={add_loss:.4f} net_loss={to_float(self.state.get('loss'), 0.0):.4f}")
                    elif realized_pnl > 0 and loss_balance > 0:
                        recover = min(loss_balance, realized_pnl)
                        self.state["loss"] = max(0.0, loss_balance - recover)
                        log(f"[LOSS-RECOVER] route=onchain token={token_name} recover={recover:.4f} net_loss={to_float(self.state.get('loss'), 0.0):.4f}")

                # 连续亏损拉黑（仅在全额卖出时统计）
                if sell_ratio >= 0.999:
                    streaks = self.state.get("onchain_loss_streak")
                    if not isinstance(streaks, dict):
                        streaks = {}
                    key = str(contract or "").strip().lower()
                    if realized_pnl < 0:
                        new_streak = int(streaks.get(key) or 0) + 1
                        streaks[key] = new_streak
                        log(f"[LOSS-STREAK] token={token_name} contract={key} streak={new_streak}")
                        if new_streak >= self.cfg.onchain_loss_streak_block:
                            self._remember_hard_block_contract(contract, token_name, f"loss_streak:{new_streak}")
                            streaks[key] = 0
                    else:
                        streaks[key] = 0
                    self.state["onchain_loss_streak"] = streaks

                if sell_ratio >= 0.999:
                    # 立刻移除，避免同一轮重复 inspect 导致重复 SELL 日志
                    positions.pop(symbol, None)
                else:
                    remaining_entry = max(0.01, entry_quote_usdt * (1.0 - sold_ratio))
                    pos["entry_quote_usdt"] = remaining_entry
                    pos["peak_quote_usdt"] = max(remaining_entry, est_quote_usdt * (1.0 - sold_ratio))
                    pos["tp1_done"] = True
                    pos["last_sell_reason"] = reason
                    pos["last_sell_ts"] = now_ts
                    positions[symbol] = pos
            except Exception as e:
                log(f"[ERR] onchain 卖出失败 token={token_name} reason={reason}: {str(e)[:160]}")

        for sym in to_remove:
            positions.pop(sym, None)
        self.state["positions"] = positions

    def _write_candidates_cache(self, candidates: list[dict[str, Any]]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        old_items: list[dict[str, Any]] = []
        if CANDIDATE_FILE.exists():
            try:
                old_payload = json.loads(CANDIDATE_FILE.read_text(encoding="utf-8"))
                old_items = old_payload.get("items") or []
                if not isinstance(old_items, list):
                    old_items = []
            except Exception:
                old_items = []

        payload = {
            "updatedAt": int(time.time()),
            "chainId": self.cfg.signal_chain_id,
            "items": candidates,
            "rawTotal": len(candidates),
        }
        CANDIDATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        old_keys = {self._candidate_key(x) for x in old_items if isinstance(x, dict)}
        new_keys = {self._candidate_key(x) for x in candidates if isinstance(x, dict)}

        added = [x for x in candidates if isinstance(x, dict) and self._candidate_key(x) not in old_keys]
        removed_count = len(old_keys - new_keys)

        if old_items:
            log(f"[SIGNALS-DELTA] new={len(added)} removed={removed_count} total={len(candidates)} raw_total={len(candidates)}")
            if added:
                preview = ", ".join(self._candidate_label(x) for x in added[:20])
                log(f"[SIGNALS-NEW] {preview}")
        else:
            log(f"[SIGNALS-DELTA] init total={len(candidates)} raw_total={len(candidates)}")

    def _read_candidates_cache(self) -> list[dict[str, Any]]:
        if not CANDIDATE_FILE.exists():
            return []
        try:
            payload = json.loads(CANDIDATE_FILE.read_text(encoding="utf-8"))
            items = (payload or {}).get("items") or []
            return items if isinstance(items, list) else []
        except Exception:
            return []

    def _run_trade_cycle(self, candidates: list[dict[str, Any]]) -> None:
        spent = 0.0 if self.cfg.dry_run else to_float(self.state.get("spent"), 0.0)
        loss_today = 0.0 if self.cfg.dry_run else to_float(self.state.get("loss"), 0.0)

        daily_cap = self.cfg.max_daily_usdt if self.cfg.max_daily_usdt > 0 else 1_000_000_000.0
        budget_left = max(0.0, daily_cap - spent)
        if not self.cfg.dry_run and self.cfg.max_daily_usdt > 0 and budget_left <= 0:
            log(f"[INFO] 今日预算已用完 spent={spent:.2f} limit={self.cfg.max_daily_usdt:.2f}")
            return

        loss_cap = self.cfg.max_daily_loss_usdt if self.cfg.max_daily_loss_usdt > 0 else 1_000_000_000.0
        loss_left = max(0.0, loss_cap - loss_today)
        if not self.cfg.dry_run and self.cfg.max_daily_loss_usdt > 0 and loss_left <= 0:
            log(f"[INFO] 今日亏损上限触发 loss={loss_today:.4f} limit={self.cfg.max_daily_loss_usdt:.4f}")
            return

        self._last_source_stats = {
            "candidates": len(candidates),
            "news_tokens": sum(1 for x in candidates if isinstance(x, dict) and bool(x.get("in_news"))),
            "follow_tokens": sum(1 for x in candidates if isinstance(x, dict) and to_int(x.get("follow_wallet_count"), 0) > 0),
        }

        log(f"[INFO] 候选数量={len(candidates)} budget_left={budget_left:.2f} loss_left={loss_left:.4f} dry_run={self.cfg.dry_run}")

        for cand in candidates:
            token = cand.get("symbol") if isinstance(cand, dict) else ""
            if not token:
                continue

            token_block_reason = self._token_name_hard_block_reason(str(token))
            if token_block_reason:
                log(f"[SKIP] token={token} route=onchain 命中硬禁止名单 reason={token_block_reason}")
                continue

            score, detail = self._score_candidate(cand)
            mapped_symbol = self._map_to_binance_symbol(str(token))
            contract = str(detail.get("contract") or "").strip()
            route = "spot" if mapped_symbol else "onchain"

            hard_block_reason = str(detail.get("hard_block_reason") or "").strip()
            if hard_block_reason:
                if route == "onchain" and contract:
                    self._remember_hard_block_contract(contract, str(token), hard_block_reason)
                log(f"[SKIP] token={token} route={route} 命中硬禁止名单 reason={hard_block_reason}")
                continue

            min_required, min_reason = self._dynamic_min_score(cand, detail, route)
            if score < min_required:
                log(f"[SKIP] token={token} route={route} score={score:.1f} < min={min_required:.1f} reason={min_reason}")
                continue

            base_quote = self.cfg.max_usdt_per_trade if self.cfg.max_usdt_per_trade > 0 else self.cfg.fallback_quote_usdt
            quote_raw, size_note = self._risk_adjusted_quote(base_quote, score, min_required, detail, route)
            if self.cfg.max_usdt_per_trade > 0:
                quote_cap = self.cfg.max_usdt_per_trade
            else:
                quote_cap = self.cfg.fallback_quote_usdt * max(1.0, self.cfg.risk_sizing_max_multiplier)
            quote = min(quote_raw, quote_cap, budget_left)
            min_quote = max(1.0, self.cfg.risk_sizing_min_quote_usdt)
            if quote < min_quote:
                log(f"[SKIP] token={token} route={route} 动态仓位过小 quote={quote:.2f} < min_quote={min_quote:.2f} note={size_note}")
                continue

            try:
                entry_loss = 0.0
                if route == "spot":
                    if not self.cfg.dry_run and (not self.cfg.binance_api_key or not self.cfg.binance_api_secret):
                        log(f"[SKIP] token={token} route=spot 缺少 BINANCE_API_KEY/SECRET")
                        continue

                    symbol = str(mapped_symbol)
                    if symbol in (self.state.get("positions") or {}):
                        continue

                    px = self._ticker_price(symbol)
                    if px <= 0:
                        continue
                    qty = quote / px
                    resp = self._place_market_buy(symbol, quote)
                    fill_price, fill_qty, fill_quote = self._extract_spot_buy_fill(resp, px, qty, quote)
                    log(f"[BUY-SPOT] {symbol} score={score:.1f} quote={quote:.2f} base={base_quote:.2f} size_note={size_note} entry={fill_price:.6f} exec_qty={fill_qty:.8f} exec_quote={fill_quote:.4f} detail={json.dumps(detail, ensure_ascii=False)[:180]} resp={json.dumps(resp, ensure_ascii=False)[:200]}")

                    self.state.setdefault("positions", {})[symbol] = {
                        "route": "spot",
                        "entry_price": fill_price,
                        "entry_quote_usdt": fill_quote,
                        "qty": fill_qty,
                        "dry_run_position": bool(self.cfg.dry_run),
                        "opened_at": int(time.time()),
                        "score": score,
                        "token": token,
                    }
                else:
                    if not contract:
                        log(f"[SKIP] token={token} route=onchain 缺少合约地址")
                        continue
                    if not self.onchain_trader.ensure_ready():
                        log(f"[SKIP] token={token} route=onchain 交易器未就绪 err={self.onchain_trader.last_error or 'unknown'}")
                        continue

                    contract_lc = contract.lower()
                    if contract_lc in self._onchain_block_contracts:
                        log(f"[SKIP] token={token} route=onchain 命中禁止补仓名单 contract={contract}")
                        continue

                    pos_key = f"ONCHAIN:{contract_lc}"
                    if pos_key in (self.state.get("positions") or {}):
                        continue

                    block_left = self._zero_amount_block_left(contract)
                    if block_left > 0:
                        log(f"[SKIP] token={token} route=onchain zero_amount冷却中 left={block_left}s contract={contract}")
                        continue

                    bnb_usdt = self._ticker_price("BNBUSDT")
                    if bnb_usdt <= 0:
                        log(f"[SKIP] token={token} route=onchain 无法获取BNBUSDT")
                        continue

                    try:
                        wallet_bnb = self.onchain_trader.native_balance_bnb()
                    except Exception as bal_e:
                        log(f"[SKIP] token={token} route=onchain 无法获取钱包BNB余额 err={str(bal_e)[:120]}")
                        continue

                    spendable_bnb = max(0.0, wallet_bnb - self.cfg.onchain_min_bnb_reserve)
                    balance_cap_bnb = spendable_bnb * self.cfg.onchain_max_wallet_usage_per_trade
                    balance_cap_usdt = balance_cap_bnb * bnb_usdt
                    if spendable_bnb <= 0 or balance_cap_usdt <= 0:
                        log(
                            f"[SKIP] token={token} route=onchain 可用BNB不足 balance={wallet_bnb:.6f} "
                            f"reserve={self.cfg.onchain_min_bnb_reserve:.6f} usage_cap={self.cfg.onchain_max_wallet_usage_per_trade:.2f}"
                        )
                        continue

                    quote_before_balance_cap = quote
                    quote = min(quote, balance_cap_usdt)
                    if quote < min_quote:
                        log(
                            f"[SKIP] token={token} route=onchain 余额约束后仓位过小 "
                            f"quote={quote:.2f} < min_quote={min_quote:.2f} balance_bnb={wallet_bnb:.6f} "
                            f"reserve={self.cfg.onchain_min_bnb_reserve:.6f} cap_usdt={balance_cap_usdt:.2f}"
                        )
                        continue
                    if quote + 1e-9 < quote_before_balance_cap:
                        log(
                            f"[SIZE-CAP] token={token} route=onchain quote {quote_before_balance_cap:.2f}->{quote:.2f} "
                            f"balance_bnb={wallet_bnb:.6f} spendable_bnb={spendable_bnb:.6f} cap_usdt={balance_cap_usdt:.2f} "
                            f"usage_cap={self.cfg.onchain_max_wallet_usage_per_trade:.2f} reserve={self.cfg.onchain_min_bnb_reserve:.6f}"
                        )

                    if self.cfg.okx_quote_probe_enabled:
                        try:
                            okx_probe = okx_quote_token(
                                token_contract=contract,
                                quote_usdt=quote,
                                bnb_price_usdt=bnb_usdt,
                                wallet_address=self.onchain_trader.wallet_address,
                                chain="bsc",
                                max_price_impact_pct=self.cfg.okx_quote_probe_max_price_impact_pct,
                                timeout_sec=self.cfg.okx_quote_probe_timeout_sec,
                            )
                            log(
                                f"[OKX-QUOTE] token={token} contract={contract} status={okx_probe.status} "
                                f"route_count={okx_probe.route_count} impact={okx_probe.price_impact_pct:.2f} "
                                f"honeypot={okx_probe.honeypot} tax={okx_probe.tax_rate:.2f}"
                            )
                            append_okx_probe_result(
                                probe_row(
                                    okx_probe,
                                    symbol=token,
                                    score=round(score, 4),
                                    route="onchain",
                                    candidate_flags={
                                        "in_alpha": bool(cand.get("in_alpha")),
                                        "in_rank": bool(cand.get("in_rank")),
                                        "in_meme": bool(cand.get("in_meme")),
                                        "in_topic": bool(cand.get("in_topic")),
                                    },
                                    liquidity=to_float(detail.get("liquidity"), 0.0),
                                    holders=to_int(detail.get("holders"), 0),
                                    riskLevel=to_int(detail.get("riskLevel"), 0),
                                    quote_usdt=round(quote, 6),
                                ),
                                OKX_QUOTE_PROBE_FILE,
                            )
                        except Exception as probe_e:
                            log(f"[OKX-QUOTE] token={token} contract={contract} status=probe_exception reason={str(probe_e)[:180]}")

                    primary_err = ""
                    if self.cfg.onchain_okx_primary_enabled:
                        try:
                            resp = self.onchain_trader.buy_via_okx(
                                contract_address=contract,
                                quote_usdt=quote,
                                bnb_price_usdt=bnb_usdt,
                                slippage_bps=self.cfg.onchain_slippage_bps,
                                dry_run=self.cfg.dry_run,
                                max_price_impact_pct=self.cfg.okx_quote_probe_max_price_impact_pct,
                                timeout_sec=self.cfg.okx_quote_probe_timeout_sec,
                                min_sellback_ratio=self.cfg.onchain_min_sellback_ratio,
                                max_entry_loss_usdt=loss_left if self.cfg.max_daily_loss_usdt > 0 else -1.0,
                            )
                        except Exception as okx_e:
                            primary_err = str(okx_e)[:180]
                            log(f"[WARN] token={token} route=onchain OKX主执行失败，回退helper: {primary_err}")
                            resp = self.onchain_trader.buy(
                                contract_address=contract,
                                quote_usdt=quote,
                                bnb_price_usdt=bnb_usdt,
                                slippage_bps=self.cfg.onchain_slippage_bps,
                                min_sellback_ratio=self.cfg.onchain_min_sellback_ratio,
                                max_entry_loss_usdt=loss_left if self.cfg.max_daily_loss_usdt > 0 else -1.0,
                                dry_run=self.cfg.dry_run,
                            )
                            resp["fallbackFrom"] = "okx"
                            resp["fallbackReason"] = primary_err
                    else:
                        resp = self.onchain_trader.buy(
                            contract_address=contract,
                            quote_usdt=quote,
                            bnb_price_usdt=bnb_usdt,
                            slippage_bps=self.cfg.onchain_slippage_bps,
                            min_sellback_ratio=self.cfg.onchain_min_sellback_ratio,
                            max_entry_loss_usdt=loss_left if self.cfg.max_daily_loss_usdt > 0 else -1.0,
                            dry_run=self.cfg.dry_run,
                        )
                    qty = to_float(resp.get("estimatedAmount"), 0.0)
                    sellback_ratio = to_float(resp.get("sellbackRatio"), 0.0)
                    entry_loss = max(0.0, to_float(resp.get("entryLossQuote"), 0.0))
                    opened_at = int(time.time())
                    position_id = f"onchain-{opened_at}-{contract_lc[-6:]}-{uuid.uuid4().hex[:6]}"
                    log(
                        f"[BUY-ONCHAIN] pos_id={position_id} token={token} score={score:.1f} quote={quote:.2f} base={base_quote:.2f} "
                        f"size_note={size_note} contract={contract} sellback={sellback_ratio:.4f} entry_loss={entry_loss:.4f} "
                        f"resp={json.dumps(resp, ensure_ascii=False)[:220]}"
                    )

                    self.state.setdefault("positions", {})[pos_key] = {
                        "route": "onchain",
                        "entry_price": 0.0,
                        "entry_quote_usdt": quote,
                        "peak_quote_usdt": quote,
                        "qty": qty,
                        "dry_run_position": bool(self.cfg.dry_run),
                        "opened_at": opened_at,
                        "position_id": position_id,
                        "score": score,
                        "token": token,
                        "contract": contract,
                        "riskLevel": to_int(detail.get("riskLevel"), 3),
                        "liquidity": to_float(detail.get("liquidity"), 0.0),
                        "holders": to_int(detail.get("holders"), 0),
                        "tp1_done": False,
                        "exec_engine": str(resp.get("engine") or "helper"),
                    }

                if not self.cfg.dry_run:
                    self.state["spent"] = spent + quote
                    spent = self.state["spent"]
                    budget_left = max(0.0, daily_cap - spent)

                    if entry_loss > 0:
                        self.state["loss"] = to_float(self.state.get("loss"), 0.0) + entry_loss
                        loss_today = self.state["loss"]
                        loss_left = max(0.0, loss_cap - loss_today)
                        log(f"[LOSS] route={route} token={token} add={entry_loss:.4f} daily_loss={loss_today:.4f}")

                    if self.cfg.max_daily_usdt > 0 and budget_left <= 0:
                        break
                    if self.cfg.max_daily_loss_usdt > 0 and loss_left <= 0:
                        log(f"[INFO] 今日亏损上限触发 loss={loss_today:.4f} limit={self.cfg.max_daily_loss_usdt:.4f}")
                        break
            except Exception as e:
                msg = str(e)[:180]
                if route == "onchain" and contract and self._looks_like_timeout_error(msg):
                    adopted = self._adopt_onchain_position_from_balance(token, contract, quote, score, detail, source="buy_timeout")
                    cooldown = self._mark_transient_error_block(contract, self._onchain_timeout_cooldown_sec)
                    if adopted:
                        if not self.cfg.dry_run:
                            self.state["spent"] = spent + quote
                            spent = self.state["spent"]
                            budget_left = max(0.0, daily_cap - spent)
                        log(
                            f"[RECOVER] token={token} route={route} 买入请求超时但检测到链上余额，"
                            f"已补录持仓并冷却{cooldown}s contract={contract}"
                        )
                    else:
                        log(
                            f"[WARN] token={token} route={route} 买入请求超时，未检出余额；"
                            f"已进入冷却{cooldown}s contract={contract} err={msg}"
                        )
                elif "estimated amount zero" in msg and route == "onchain" and contract:
                    cooldown = self._mark_zero_amount_block(contract)
                    log(f"[SKIP] token={token} route={route} 过滤estimated_amount_zero，冷却{cooldown}s contract={contract}")
                elif "daily_loss_guard" in msg or "sellback_ratio_too_low" in msg or "trySell" in msg:
                    log(f"[SKIP] token={token} route={route} 风控拦截: {msg}")
                else:
                    log(f"[ERR] 买入失败 token={token} route={route}: {msg}")

    def run_once(self) -> None:
        self.state = self._load_state()
        self._apply_auto_evolve_from_state()
        self._reset_daily_budget_if_needed()

        mode = self.cfg.mode

        if mode in {"trade", "all"}:
            if not self.cfg.dry_run:
                try:
                    self._reconcile_onchain_orphan_positions()
                except Exception as e:
                    log(f"[WARN] onchain 持仓补录失败: {str(e)[:120]}")

            try:
                self._manage_positions()
            except Exception as e:
                log(f"[WARN] onchain 持仓管理失败: {str(e)[:120]}")

            self._save_state()

        if self.cfg.watch_address and mode in {"positions", "signals", "trade", "all"}:
            try:
                positions = self.skills.address_positions(self.cfg.signal_chain_id, self.cfg.watch_address)
                log(f"[ADDR] watch={self.cfg.watch_address[:6]}... positions={len(positions)}")
            except Exception as e:
                log(f"[WARN] 地址持仓查询失败: {str(e)[:100]}")

        if mode == "positions":
            return

        candidates: list[dict[str, Any]] = []
        if mode in {"signals", "all"}:
            candidates = self._candidate_symbols()
            self._write_candidates_cache(candidates)
            log(f"[SIGNALS] 产出候选={len(candidates)}")

        if mode == "signals":
            return

        if mode == "trade":
            candidates = self._read_candidates_cache()
            if not candidates:
                candidates = self._candidate_symbols()
                log("[TRADE] 候选缓存为空，退化为实时抓取")

        if mode in {"trade", "all"}:
            self._run_trade_cycle(candidates)
            self._maybe_auto_evolve()
            self._save_state()


def main() -> int:
    cfg = load_config()
    if not cfg.enabled:
        log("BINANCE_BOT_ENABLED=false，退出")
        return 0

    log(
        "启动 Binance AutoTrader | mode=%s dry_run=%s testnet=%s max_trade=%.2f max_daily=%.2f max_daily_loss=%.4f fallback_quote=%.2f risk_sizing=%s risk_min_quote=%.2f risk_max_mul=%.2f min_score=%.1f dyn_min=%s floor=%.1f rank_pages=%d rank_page_size=%d follow_wallets=%d follow_auto=%s follow_auto_limit=%d topN=%d news=%s news_limit=%d news_max_age=%ds okx_primary=%s okx_probe=%s evolve=%s evolve_apply_state=%s onchain_slippage_bps=%d min_sellback=%.3f zero_amt_cd=%ds onchain_sell=%s tp=%.1f sl=%.1f trail=%.1f panic5m=%.1f panic1h=%.1f min_hold=%ds max_hold=%dm stagnation=%s stg_hold=%dm stg_low_liq_hold=%dm stg_liq<=%.0f stg_holders<=%d stg_vol1h<=%.0f stg_vol5m<=%.0f stg_abs5m<=%.2f stg_abs1h<=%.2f stg_pnl<=%.2f evolve_ivl=%ds evolve_win=%dm hard_block_kw=%d tp1_ratio=%.2f tp2_mul=%.2f watch=%s"
        % (
            cfg.mode,
            cfg.dry_run,
            cfg.testnet,
            cfg.max_usdt_per_trade,
            cfg.max_daily_usdt,
            cfg.max_daily_loss_usdt,
            cfg.fallback_quote_usdt,
            cfg.risk_sizing_enabled,
            cfg.risk_sizing_min_quote_usdt,
            cfg.risk_sizing_max_multiplier,
            cfg.min_score,
            cfg.dynamic_min_score_enabled,
            cfg.dynamic_min_score_floor,
            cfg.rank_pages,
            cfg.rank_page_size,
            len(cfg.follow_wallet_addresses),
            cfg.smart_wallet_auto_collect,
            cfg.smart_wallet_auto_limit,
            cfg.follow_wallet_top_n,
            cfg.news_enabled,
            cfg.news_limit,
            cfg.news_max_age_sec,
            cfg.onchain_okx_primary_enabled,
            cfg.okx_quote_probe_enabled,
            cfg.auto_evolve_enabled,
            cfg.auto_evolve_apply_state,
            cfg.onchain_slippage_bps,
            cfg.onchain_min_sellback_ratio,
            cfg.onchain_zero_amount_cooldown_sec,
            cfg.onchain_sell_enabled,
            cfg.onchain_take_profit_pct,
            cfg.onchain_stop_loss_pct,
            cfg.onchain_trailing_stop_pct,
            cfg.onchain_panic_drop_5m_pct,
            cfg.onchain_panic_drop_1h_pct,
            cfg.onchain_min_hold_seconds,
            cfg.onchain_max_hold_minutes,
            cfg.onchain_stagnation_sell_enabled,
            cfg.onchain_stagnation_min_hold_minutes,
            cfg.onchain_stagnation_low_liq_hold_minutes,
            cfg.onchain_stagnation_liq_threshold,
            cfg.onchain_stagnation_holder_threshold,
            cfg.onchain_stagnation_max_volume_1h_usdt,
            cfg.onchain_stagnation_max_volume_5m_usdt,
            cfg.onchain_stagnation_max_abs_change_5m_pct,
            cfg.onchain_stagnation_max_abs_change_1h_pct,
            cfg.onchain_stagnation_max_pnl_pct,
            cfg.auto_evolve_interval_sec,
            cfg.auto_evolve_window_minutes,
            len(cfg.onchain_hard_block_keywords),
            cfg.onchain_tp_partial_ratio,
            cfg.onchain_tp_second_multiplier,
            (cfg.watch_address[:10] + "...") if cfg.watch_address else "none",
        )
    )
    log(
        f"[SOURCE-CFG] opennews={cfg.news_enabled} opennews_limit={cfg.news_limit} opennews_cache={cfg.news_cache_sec}s "
        f"square_news={cfg.square_news_enabled} square_limit={cfg.square_news_limit} square_cache={cfg.square_news_cache_sec}s "
        f"square_cookie={'yes' if cfg.square_news_cookie_header else 'no'} square_csrf={'yes' if cfg.square_news_csrf_token else 'no'}"
    )
    log(
        f"[EVOLVE-CFG] mode=openclaw-cli enabled={cfg.auto_evolve_enabled} model=openclaw-main interval={cfg.auto_evolve_interval_sec}s "
        f"window={cfg.auto_evolve_window_minutes}m candidate_sample={cfg.auto_evolve_candidate_sample_size} live_refresh={cfg.auto_evolve_live_source_refresh} "
        f"review_file={EVOLVE_REVIEW_FILE}"
    )

    # 真实交易模式下：若缺少 API Key/Secret，spot 路由不可用，但 onchain 路由仍可运行
    if not cfg.dry_run and (not cfg.binance_api_key or not cfg.binance_api_secret):
        log("[WARN] 未配置 BINANCE_API_KEY/BINANCE_API_SECRET，spot 路由将跳过，仅执行 onchain 路由")

    spot = BinanceSpotClient(cfg)
    skills = BinanceSkillsHubClient()
    engine = StrategyEngine(cfg, spot, skills)

    while True:
        try:
            engine.run_once()
        except KeyboardInterrupt:
            log("收到中断，退出")
            return 0
        except Exception as e:
            if _is_timeout_error(e):
                send_timeout_telegram_alert(f"binance_loop:{cfg.mode}", str(e))
            log(f"[LOOP-ERR] {str(e)[:180]}")
        time.sleep(cfg.poll_interval_sec)


if __name__ == "__main__":
    sys.exit(main())
