#!/usr/bin/env python3
"""Polymarket CLOB V2 readiness checks for PolyBot.

This script is read-only by default: it does not place orders, cancel orders,
wrap USDC.e, or update allowances. Use it before any live run to catch CLOB V2,
pUSD, approval, and environment issues early.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import os
import sys
from dataclasses import dataclass
from typing import Any, cast

from dotenv import load_dotenv

# Allow running as: python3 scripts/preflight_v2.py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from executor import Executor  # noqa: E402
from market import get_current_market  # noqa: E402
from proxy import apply_proxy, check_proxy, ensure_tor  # noqa: E402
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams  # noqa: E402
from security import redact_secret, sanitize_exception_text  # noqa: E402


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = False


class Reporter:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, ok: bool, detail: str = "", fatal: bool = False) -> None:
        self.checks.append(Check(name, ok, detail, fatal))
        icon = "✅" if ok else ("❌" if fatal else "⚠️ ")
        suffix = f" — {detail}" if detail else ""
        print(f"{icon} {name}{suffix}")

    def exit_code(self) -> int:
        return 1 if any((not c.ok and c.fatal) for c in self.checks) else 0


def pkg_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def summarize_balance_allowance(data: dict[str, Any]) -> str:
    balance = safe_float(data.get("balance", 0)) / 1e6
    allowance_raw = data.get("allowance")
    if allowance_raw is None:
        return f"balance=${balance:.2f}; allowance=<not returned>"
    allowance = safe_float(allowance_raw) / 1e6
    return f"balance=${balance:.2f}; allowance=${allowance:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only CLOB V2/pUSD preflight checks")
    parser.add_argument("--with-tor", action="store_true", help="start/apply Tor proxy before CLOB checks")
    parser.add_argument("--check-proxy", action="store_true", help="verify Tor exit IP via httpbin when --with-tor is used")
    parser.add_argument("--min-balance", type=float, default=5.0, help="minimum collateral balance expected for a live smoke test")
    args = parser.parse_args()

    load_dotenv(os.path.join(ROOT, ".env"))
    r = Reporter()

    print("\nPolyBot CLOB V2 preflight")
    print("=" * 34)

    v2_version = pkg_version("py-clob-client-v2")
    v1_version = pkg_version("py-clob-client")
    r.add("py-clob-client-v2 installed", bool(v2_version), v2_version or "missing", fatal=True)
    r.add("legacy py-clob-client absent", not bool(v1_version), f"installed={v1_version}" if v1_version else "not installed")

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    private_key = os.getenv("PRIVATE_KEY", "")
    safe_address = os.getenv("SAFE_ADDRESS", "")
    r.add(".env loaded", bool(private_key), "PRIVATE_KEY present" if private_key else "PRIVATE_KEY missing", fatal=True)
    r.add("Safe/proxy wallet configured", bool(safe_address), redact_secret(safe_address) if safe_address else "SAFE_ADDRESS missing")
    r.add("DRY_RUN setting", True, str(dry_run))

    if args.with_tor:
        try:
            proxy_url = ensure_tor()
            apply_proxy(proxy_url)
            r.add("Tor proxy applied for CLOB/httpx", True, proxy_url)
            if args.check_proxy:
                r.add("Tor proxy connectivity", check_proxy(proxy_url), "httpbin.org/ip")
        except Exception as e:
            r.add("Tor proxy setup", False, sanitize_exception_text(e), fatal=True)
            return r.exit_code()
    else:
        r.add("Tor proxy", True, "not requested; CLOB checks use direct connection")

    market = None
    try:
        market = get_current_market()
        r.add("current BTC 5m market discovered", bool(market), market.slug if market else "none", fatal=True)
        if market:
            r.add("Gamma token IDs parsed", bool(market.token_id_up and market.token_id_down),
                  f"UP {market.token_id_up[:10]}… / DOWN {market.token_id_down[:10]}…", fatal=True)
            r.add("Gamma conditionId parsed", bool(market.condition_id), market.condition_id[:18] + "…" if market.condition_id else "missing")
    except Exception as e:
        r.add("current market discovery", False, sanitize_exception_text(e), fatal=True)
        return r.exit_code()

    executor = Executor(private_key=private_key, safe_address=safe_address, dry_run=dry_run)
    r.add("executor initialize", executor.initialize(), "py_clob_client_v2", fatal=True)
    if not executor.client:
        return r.exit_code()

    try:
        ok = executor.client.get_ok()
        r.add("CLOB health get_ok", bool(ok), str(ok), fatal=True)
    except Exception as e:
        r.add("CLOB health get_ok", False, sanitize_exception_text(e), fatal=True)

    try:
        params = BalanceAllowanceParams(asset_type=cast(Any, AssetType.COLLATERAL))
        raw = cast(dict[str, Any], executor.client.get_balance_allowance(params))
        detail = summarize_balance_allowance(raw)
        balance = safe_float(raw.get("balance", 0)) / 1e6
        r.add("pUSD/collateral balance endpoint", True, detail, fatal=True)
        r.add("minimum live-test balance", balance >= args.min_balance,
              f"${balance:.2f} >= ${args.min_balance:.2f}" if balance >= args.min_balance else f"${balance:.2f} < ${args.min_balance:.2f}",
              fatal=False)
    except Exception as e:
        r.add("pUSD/collateral balance endpoint", False, sanitize_exception_text(e), fatal=True)

    if market:
        for label, token_id in [("UP", market.token_id_up), ("DOWN", market.token_id_down)]:
            try:
                token_params = BalanceAllowanceParams(asset_type=cast(Any, AssetType.CONDITIONAL), token_id=token_id)
                raw = cast(dict[str, Any], executor.client.get_balance_allowance(token_params))
                r.add(f"conditional balance endpoint {label}", True, summarize_balance_allowance(raw))
            except Exception as e:
                r.add(f"conditional balance endpoint {label}", False, sanitize_exception_text(e))

        try:
            info = executor.client.get_clob_market_info(market.condition_id)
            mts = info.get("mts") if isinstance(info, dict) else None
            mos = info.get("mos") if isinstance(info, dict) else None
            fd = info.get("fd") if isinstance(info, dict) else None
            r.add("CLOB market info", True, f"mts={mts}; mos={mos}; fd={fd}", fatal=True)
        except Exception as e:
            r.add("CLOB market info", False, sanitize_exception_text(e), fatal=True)

        for label, token_id in [("UP", market.token_id_up), ("DOWN", market.token_id_down)]:
            try:
                buy_price = executor.get_market_price(token_id, "BUY", args.min_balance)
                sell_price = executor.get_market_price(token_id, "SELL", args.min_balance)
                r.add(f"CLOB complement price {label}", buy_price > 0 or sell_price > 0,
                      f"BUY={buy_price:.3f}; SELL={sell_price:.3f}")
            except Exception as e:
                r.add(f"CLOB complement price {label}", False, sanitize_exception_text(e))

    print("=" * 34)
    if r.exit_code() == 0:
        print("✅ Preflight completed: no fatal blockers found.")
        print("   This did NOT place/cancel orders or wrap/approve funds.")
    else:
        print("❌ Preflight found fatal blockers. Do not run live trading yet.")
    return r.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
