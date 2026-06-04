"""Polymarket CLOB order execution.

Live entries use marketable FAK BUY limit orders with integer share size. This
keeps taker execution while avoiding the py-clob-client-v2 BUY market helper's
amount/price float division path.
"""

import csv
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional, Any

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgsV2,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client_v2.constants import POLYGON
from security import redact_secret, sanitize_exception_text


FILLED = "FILLED"
PARTIAL = "PARTIAL"
REJECTED = "REJECTED"
FAILED = "FAILED"

MIN_SHARES = 1.0
MIN_AMOUNT_USD = 1.0
MAX_BUY_PRICE = 0.90
POLY_MIN_ORDER_SHARES = 5.0


@dataclass
class MarketMetadata:
    # Polymarket CLOB `minimum_order_size` is a share count, not a fixed USDC
    # notional. BTC 5m markets commonly return 5, meaning 5 outcome tokens.
    minimum_order_size: float = POLY_MIN_ORDER_SHARES
    minimum_tick_size: float = 0.01
    fee_rate_bps: float = 0.0
    # CLOB fee = (amount/price) * rate * (p*(1-p))**exponent. Verified live
    # fd={'r':0.07,'e':1}. The sizing model (effective_market_price) assumes
    # exponent==1; surface the real exponent so a future e!=1 is caught, not
    # silently mismodeled.
    fee_exponent: float = 1.0
    maker_base_fee: float = 0.0
    taker_base_fee: float = 0.0


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0
    amount_usd: float = 0.0
    shares: float = 0.0
    shares_remaining: float = 0.0
    token_id: str = ""
    error: str = ""
    dry_run: bool = True
    # Accounting semantics. Keep raw model sizing, planned order notional, and
    # wallet cashflow separate. Polymarket fees make cash spent > shares*price;
    # never infer token shares from fee-inclusive cash spent.
    raw_kelly_usd: float = 0.0
    planned_order_notional_usd: float = 0.0
    actual_cash_spent_usd: float = 0.0
    estimated_fee_usd: float = 0.0
    sizing_reason: str = ""
    # Hot-path latency breakdown (ms). bal = pre-sign collateral GET (0 when a
    # balance_hint skips it), sign = create_order EIP-712 build, post = post_order
    # round-trip (network + server-side FAK match, inseparable client-side),
    # submit_ack = sign + post = the controllable book-read -> order-land window.
    bal_ms: float = 0.0
    sign_ms: float = 0.0
    post_ms: float = 0.0
    submit_ack_ms: float = 0.0


@dataclass
class MinimumLotPlan:
    executable: bool
    reason: str
    shares: int = 0
    amount_usd: float = 0.0
    raw_kelly_usd: float = 0.0
    minimum_order_shares: float = 0.0
    minimum_cost_usd: float = 0.0


def _decimal_from_float(value: float | int | str) -> Decimal:
    """Convert numeric inputs through str() so 0.59 stays Decimal('0.59')."""
    return Decimal(str(value))


def _fmt_ts(value) -> str:
    try:
        return f"{float(value):.6f}"
    except Exception:
        return ""


def _fmt_float(value) -> str:
    try:
        return f"{float(value):.1f}"
    except Exception:
        return ""


def calculate_order_size(price: float, max_usd: float) -> tuple[float, float]:
    """Return integer shares and exact 2-decimal collateral spend."""
    if price <= 0 or max_usd <= 0:
        return 0.0, 0.0

    price_dec = _decimal_from_float(price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    max_usd_dec = _decimal_from_float(max_usd).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if price_dec <= 0 or max_usd_dec <= 0:
        return 0.0, 0.0

    price_cents = int(price_dec * 100)
    max_usd_cents = int(max_usd_dec * 100)
    max_shares = max_usd_cents // price_cents if price_cents > 0 else 0

    if max_shares < MIN_SHARES:
        min_cost_cents = int(MIN_SHARES) * price_cents
        if min_cost_cents <= max_usd_cents:
            max_shares = int(MIN_SHARES)
        else:
            return 0.0, 0.0

    shares = int(max_shares)
    spend_dec = (Decimal(shares) * price_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if shares < MIN_SHARES or spend_dec <= 0:
        return 0.0, 0.0
    return float(shares), float(spend_dec)


def plan_minimum_lot_order(
    *,
    price: float,
    raw_kelly_usd: float,
    minimum_order_shares: float,
    max_bet_usd: float,
    max_floor_to_kelly_ratio: float,
) -> MinimumLotPlan:
    """Plan a Polymarket minimum-share-lot BUY.

    The CLOB minimum is a share count. With small bankrolls, fractional Kelly
    often produces a dollar budget below the executable 5-share floor. Treat
    Kelly as a sanity check, then size in whole shares.
    """
    raw_kelly_usd = round(float(raw_kelly_usd or 0.0), 2)
    price_dec = _decimal_from_float(price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    min_shares = int(float(minimum_order_shares or 0.0))
    max_bet_usd = round(float(max_bet_usd or 0.0), 2)
    ratio = float(max_floor_to_kelly_ratio or 0.0)

    if price_dec <= 0 or min_shares <= 0 or max_bet_usd <= 0:
        return MinimumLotPlan(False, "invalid_minimum_lot_inputs", raw_kelly_usd=raw_kelly_usd)
    if raw_kelly_usd <= 0:
        return MinimumLotPlan(False, "kelly_size_zero", raw_kelly_usd=raw_kelly_usd)

    minimum_cost_dec = (Decimal(min_shares) * price_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    minimum_cost = float(minimum_cost_dec)
    if minimum_cost > max_bet_usd + 1e-9:
        return MinimumLotPlan(
            False,
            "minimum_share_lot_exceeds_max_bet",
            raw_kelly_usd=raw_kelly_usd,
            minimum_order_shares=float(min_shares),
            minimum_cost_usd=minimum_cost,
        )
    if ratio > 0 and minimum_cost > raw_kelly_usd * ratio + 1e-9:
        return MinimumLotPlan(
            False,
            "minimum_share_lot_exceeds_kelly_ratio",
            raw_kelly_usd=raw_kelly_usd,
            minimum_order_shares=float(min_shares),
            minimum_cost_usd=minimum_cost,
        )

    budget = min(max_bet_usd, max(minimum_cost, raw_kelly_usd))
    shares, spend = calculate_order_size(float(price_dec), budget)
    shares_i = int(shares)
    if shares_i < min_shares:
        shares_i = min_shares
        spend = minimum_cost

    reason = "minimum_share_lot" if spend <= raw_kelly_usd + 1e-9 else "raised_to_minimum_share_lot"
    return MinimumLotPlan(
        True,
        reason,
        shares=shares_i,
        amount_usd=round(float(spend), 2),
        raw_kelly_usd=raw_kelly_usd,
        minimum_order_shares=float(min_shares),
        minimum_cost_usd=minimum_cost,
    )


def _is_fak_no_fill_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "status_code=400" in text
        and "fak" in text
        and (
            "no orders found to match" in text
            or "no match" in text
            or "no matching orders" in text
        )
    )


def _is_geoblock_403(exc: Exception) -> bool:
    """Detect a Cloudflare/geoblock HTTP 403 on the CLOB order endpoint.

    Direct datacenter-IP placement normally returns 401 (auth) or 400 (FAK);
    a 403 means Cloudflare's bot/geo heuristic blocked the request, which is the
    only failure mode where routing through Tor actually helps. We do NOT treat
    401/400 as a Tor trigger.
    """
    text = str(exc).lower()
    return (
        "status_code=403" in text
        or "403 forbidden" in text
        or ("403" in text and "forbidden" in text)
        or "cloudflare" in text
    )


class Executor:
    def __init__(self, private_key: str, safe_address: str = "", dry_run: bool = True):
        self.dry_run = dry_run
        self.private_key = private_key
        self.safe_address = safe_address
        self.client: Optional[ClobClient] = None
        self._initialized = False
        self.min_order_size = POLY_MIN_ORDER_SHARES
        self.tick_size = 0.01
        self.fee_rate_bps = 0.0
        self._tor_active = False

    def initialize(self) -> bool:
        try:
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.private_key,
                chain_id=POLYGON,
                funder=self.safe_address if self.safe_address else None,
                signature_type=2 if self.safe_address else 0,
            )
            self.client.set_api_creds(self.client.create_or_derive_api_key())
            self._initialized = True
            print(f"[executor] Initialized py_clob_client_v2 ({'DRY RUN' if self.dry_run else 'LIVE'})")
            print(f"[executor] Max buy price: ${MAX_BUY_PRICE:.2f}")
            print(f"[executor] Wallet: {redact_secret(self.client.get_address())}")
            if self.safe_address:
                print(f"[executor] Safe/proxy wallet: {redact_secret(self.safe_address)}")
            return True
        except Exception as e:
            print(f"[executor] Init failed: {sanitize_exception_text(e)}")
            return False

    def _activate_tor_fallback(self) -> bool:
        """Lazily start Tor and re-route the CLOB client through it.

        Called ONLY when a live order returns HTTP 403 (Cloudflare/geo block).
        The proxy patch (proxy.apply_proxy) only affects httpx.Client instances
        created AFTER it runs, so we must rebuild the ClobClient afterwards for
        its internal session to use the SOCKS5 proxy. Idempotent: once active,
        the session stays on Tor (no flapping back to direct).
        """
        if self._tor_active:
            return True
        try:
            from proxy import ensure_tor, apply_proxy
            print("  🧅 CF 403 on order — activating Tor fallback "
                  "(first time ~60s bootstrap)...")
            proxy_url = ensure_tor()
            apply_proxy(proxy_url)
            # Rebuild the client so its httpx session picks up the proxy patch.
            if not self.initialize():
                print("  ❌ Tor fallback: CLOB client re-init failed")
                return False
            self._tor_active = True
            print(f"  ✅ Tor fallback active: {proxy_url}")
            return True
        except Exception as e:
            print(f"  ❌ Tor fallback failed: {sanitize_exception_text(e)}")
            return False

    def get_collateral_balance(self) -> float:
        if not self._initialized:
            return 0.0
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            return float(bal.get("balance", 0)) / 1e6
        except Exception as e:
            print(f"[executor] Collateral balance check failed: {sanitize_exception_text(e)}")
            return 0.0

    def get_market_metadata(self, condition_id: str) -> MarketMetadata:
        """Fetch and cache CLOB market metadata: mos, mts, and platform fee rate.

        CLOB V2 exposes compact keys via get_clob_market_info(condition_id):
        - mos: minimum order size
        - mts: minimum tick size
        - fd.r: platform fee rate used in fee = C × feeRate × p × (1-p)
        """
        if not self._initialized or not self.client or not condition_id:
            return MarketMetadata()
        try:
            info = self.client.get_clob_market_info(condition_id)
            mos = float(info.get("mos") or POLY_MIN_ORDER_SHARES)
            mts = float(info.get("mts") or 0.01)
            fd = info.get("fd") or {}
            fee_rate = float(fd.get("r") or 0.0)
            fee_exponent = float(fd.get("e") if fd.get("e") is not None else 1.0)
            if fee_exponent != 1.0:
                # effective_market_price() in strategy assumes exponent==1; a
                # different exponent would over/understate fees in sizing/edge.
                print(f"[executor] ⚠️  CLOB fee exponent={fee_exponent} (expected 1) "
                      f"— sizing model assumes 1; fee estimate may be off")
            metadata = MarketMetadata(
                minimum_order_size=mos,
                minimum_tick_size=mts,
                fee_rate_bps=round(fee_rate * 10_000.0, 6),
                fee_exponent=fee_exponent,
                maker_base_fee=float(info.get("mbf") or 0.0),
                taker_base_fee=float(info.get("tbf") or 0.0),
            )
            self.min_order_size = metadata.minimum_order_size
            self.tick_size = metadata.minimum_tick_size
            self.fee_rate_bps = metadata.fee_rate_bps
            return metadata
        except Exception as e:
            print(f"[executor] Market metadata check failed: {sanitize_exception_text(e)}")
            return MarketMetadata(
                minimum_order_size=self.min_order_size,
                minimum_tick_size=self.tick_size,
                fee_rate_bps=self.fee_rate_bps,
            )

    def _round_price_to_tick(self, price: float) -> float:
        tick = _decimal_from_float(self.tick_size or 0.01)
        if tick <= 0:
            tick = Decimal("0.01")
        price_dec = _decimal_from_float(price)
        ticks = (price_dec / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        rounded = (ticks * tick).quantize(tick, rounding=ROUND_HALF_UP)
        min_price = tick
        max_price = Decimal("1") - tick
        bounded = max(min_price, min(max_price, rounded))
        return float(bounded)

    def _estimate_fee(self, shares: float, price: float) -> float:
        """Mirror the live CLOB fee formula: fee = shares * rate * p * (1 - p).

        Same model as strategy.effective_market_price (per-share fee =
        feeRate * p * (1-p)). With fee_rate_bps == 0 (current BTC 5m) this is
        0.0; it tracks automatically if Polymarket turns fees on, so a dry-run
        fill carries the same fee drag a live fill would.
        """
        fee_rate = max(0.0, float(self.fee_rate_bps or 0.0)) / 10_000.0
        return round(float(shares) * fee_rate * price * (1.0 - price), 2)

    def _size_buy_or_reject(
        self, token_id: str, market_price: float, amount_usd: float,
    ) -> tuple[float, float, Optional[OrderResult]]:
        """Cap + integer-share sizing + minimum-lot gate, shared by DRY and LIVE.

        Returns (shares, planned_spend, None) when executable, else
        (0.0, 0.0, <rejecting OrderResult>). Keeping this identical for both
        modes guarantees a dry-run fill is only logged for an order the live
        path would also have accepted (same price cap, same share minimum, same
        integer-share rounding).
        """
        if market_price > MAX_BUY_PRICE:
            return 0.0, 0.0, OrderResult(
                success=False, status=REJECTED,
                error=f"Price ${market_price:.3f} > cap ${MAX_BUY_PRICE:.2f}",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        shares, planned_spend = calculate_order_size(market_price, amount_usd)
        if shares < 1 or planned_spend <= 0:
            return 0.0, 0.0, OrderResult(
                success=False, status=REJECTED,
                error=f"Can't afford 1 share at ${market_price:.3f} "
                      f"within ${amount_usd:.2f}",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        if shares < self.min_order_size:
            return 0.0, 0.0, OrderResult(
                success=False, status=REJECTED,
                error=f"Size {shares:.0f} shares < {self.min_order_size:.0f} min",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        return shares, planned_spend, None

    def get_market_price(self, token_id: str, side: str, amount_usd: float) -> float:
        if not self._initialized or not self.client:
            return 0.0
        try:
            price = self.client.calculate_market_price(
                token_id=token_id,
                side=side,
                amount=amount_usd,
                order_type=OrderType.GTC,
            )
            return float(price) if price else 0.0
        except Exception as e:
            err = str(e).lower()
            if "no match" not in err and "none" not in err:
                print(f"[executor] Price check failed: {sanitize_exception_text(e)}")
            return 0.0

    def warm_order_metadata(self, token_ids: list[str] | tuple[str, ...] | set[str]) -> dict[str, Any]:
        """Prime SDK metadata/version caches at new-window time, outside buy hot path.

        py-clob-client-v2 resolves tick size, neg-risk and signing version lazily.
        Let that happen when the market rolls, not between signal_ready and FAK POST.
        This method does not sign or post orders.
        """
        status: dict[str, Any] = {"ok": False, "tokens": 0, "errors": []}
        if not self._initialized or not self.client:
            status["errors"].append("not_initialized")
            return status
        warmed = 0
        try:
            resolver = getattr(self.client, "_ClobClient__resolve_version", None)
            if callable(resolver):
                resolver()
        except Exception as e:
            status["errors"].append(f"version:{sanitize_exception_text(e)}")
        for token_id in [str(t) for t in token_ids if t]:
            try:
                tick = self.client.get_tick_size(token_id)
                if tick:
                    self.tick_size = float(tick)
            except Exception as e:
                status["errors"].append(f"tick:{token_id[:8]}:{sanitize_exception_text(e)}")
            try:
                neg = self.client.get_neg_risk(token_id)
                # The call is the warmup. Value is SDK-internal metadata; store
                # nothing because the order builder asks SDK again when signing.
                _ = neg
            except Exception as e:
                status["errors"].append(f"neg:{token_id[:8]}:{sanitize_exception_text(e)}")
            warmed += 1
        status["tokens"] = warmed
        status["ok"] = warmed > 0 and not status["errors"]
        return status

    def buy(self, token_id: str, amount_usd: float, price: float = 0.0,
            balance_hint: float = -1.0, timing: Optional[dict[str, Any]] = None) -> OrderResult:
        """Buy with a marketable FAK limit order and explicit worst-price cap.

        This avoids BUY MarketOrderArgsV2(amount=USD), whose internal
        amount/price division can produce invalid share precision.

        balance_hint: caller-supplied collateral balance to skip the hot-path
        get_collateral_balance() CLOB GET (~250ms) before signing. Pass the
        window-open on-chain balance — accurate because no position opens until
        this buy. When <=0, a live balance is fetched (cold start / safety).
        """
        amount_usd = round(float(amount_usd), 2)
        if amount_usd < MIN_AMOUNT_USD:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Amount ${amount_usd:.2f} below min", side="BUY",
            )

        # Resolve the marketable price. DRY and LIVE share the sizing path below;
        # they diverge ONLY here (DRY cannot probe the book, so it requires a
        # caller-supplied price) and at the submit/verify step at the end.
        if price > 0:
            market_price = self._round_price_to_tick(price)
        elif self.dry_run:
            # No book probe in dry_run (no client). The live path resolves an
            # unknown price via get_market_price(); dry_run cannot, so a missing
            # caller price is a hard reject — never fabricate a fill price.
            return OrderResult(
                success=False, status=REJECTED,
                error="DRY buy requires a caller-supplied price (no book probe)",
                side="BUY", token_id=token_id[:16] + "...",
            )
        else:
            if not self._initialized or not self.client:
                return OrderResult(success=False, status=FAILED, error="Not initialized")
            market_price = self.get_market_price(token_id, "BUY", amount_usd)
            if market_price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get market price", side="BUY",
                    token_id=token_id[:16] + "...",
                )
            market_price = self._round_price_to_tick(market_price)

        # Shared cap + integer-share sizing + minimum-lot gate (identical DRY/LIVE).
        shares, planned_spend, reject = self._size_buy_or_reject(
            token_id, market_price, amount_usd,
        )
        if reject is not None:
            return reject

        if self.dry_run:
            # Simulated fill: same shares/price/notional the live path would
            # produce. estimated_fee mirrors the live fee formula; cash spent is
            # notional + fee so a dry-run bankroll draw matches a live one.
            planned_notional = planned_spend
            estimated_fee = self._estimate_fee(shares, market_price)
            cash_spent = round(planned_notional + estimated_fee, 2)
            return OrderResult(
                success=True, order_id=f"DRY-{int(time.time())}",
                status=FILLED, side="BUY", price=market_price,
                amount_usd=cash_spent, shares=float(int(shares)),
                token_id=token_id[:16] + "...", dry_run=True,
                planned_order_notional_usd=planned_notional,
                actual_cash_spent_usd=cash_spent,
                estimated_fee_usd=estimated_fee,
            )

        if not self._initialized or not self.client:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # Do not print between final book snapshot and POST. Console I/O is small
        # but pointless latency in the exact race window. Print after ack/fail.

        # Hot-path latency: get_collateral_balance() is a fresh ~250ms CLOB GET
        # on every order, in series before signing. With a caller hint (window-
        # open on-chain balance) we skip it, cutting book-read -> order-land
        # latency ~in half so the ask walks less before our FAK lands. The hint
        # is the window high-water balance (>= actual at submit, since balance
        # only drops on this buy), so the SDK fee-buffer never over-shrinks; the
        # post-order verify + window-boundary sync remain the source of truth.
        bal_ms = 0.0
        if balance_hint > 0:
            balance_before = balance_hint
        else:
            _b0 = time.perf_counter()
            balance_before = self.get_collateral_balance()
            bal_ms = (time.perf_counter() - _b0) * 1000.0

        # Latency instrumentation: the no-fills are a race — the book walks away
        # between our book-read and our FAK landing at the matcher. Split the hot
        # path into sign (local) vs post (network + match) so we know which part
        # to cut. Vars live outside the try so the no-fill/error paths can log too.
        sign_ms = 0.0
        post_ms = 0.0
        _post_started = False
        timing = dict(timing or {})
        timing.setdefault("executor_buy_start_ts", time.time())

        try:
            clob_order_type = OrderType.FAK
            order_args = OrderArgsV2(
                token_id=token_id,
                price=market_price,
                size=float(int(shares)),
                side="BUY",
                user_usdc_balance=balance_before,
            )
            try:
                _s0 = time.perf_counter()
                timing["sign_start_ts"] = time.time()
                signed_order = self.client.create_order(order_args)
                timing["sign_end_ts"] = time.time()
                sign_ms = (time.perf_counter() - _s0) * 1000.0
                _post_started = True
                _p0 = time.perf_counter()
                timing["post_start_ts"] = time.time()
                result = self.client.post_order(signed_order, clob_order_type, False)
                timing["post_end_ts"] = time.time()
                post_ms = (time.perf_counter() - _p0) * 1000.0
            except Exception as post_exc:
                # post_order raised (incl. FAK no-fill 400) — capture how long it
                # took before re-raising, so the no-fill latency still gets logged.
                if _post_started:
                    timing.setdefault("post_end_ts", time.time())
                    post_ms = (time.perf_counter() - _p0) * 1000.0
                # Direct placement blocked by Cloudflare (403) — activate Tor
                # for THIS order and re-submit once. Any other error (incl. FAK
                # no-fill 400) falls through to the outer handler unchanged.
                if _is_geoblock_403(post_exc) and self._activate_tor_fallback():
                    print("  🔁 Re-submitting this order via Tor (CF 403)...")
                    _s0 = time.perf_counter()
                    timing["tor_resign_start_ts"] = time.time()
                    signed_order = self.client.create_order(order_args)
                    timing["tor_resign_end_ts"] = time.time()
                    sign_ms = (time.perf_counter() - _s0) * 1000.0
                    _p0 = time.perf_counter()
                    timing["tor_post_start_ts"] = time.time()
                    result = self.client.post_order(signed_order, clob_order_type, False)
                    timing["tor_post_end_ts"] = time.time()
                    post_ms = (time.perf_counter() - _p0) * 1000.0
                else:
                    raise

            order_id = result.get("orderID", "")
            print(f"  📊 FAK submitted: ${market_price:.3f}/share "
                  f"→ {int(shares)} shares for ${planned_spend:.2f}")
            if not order_id:
                self._log_order_latency(
                    token_id, market_price, shares, "no_order_id",
                    bal_ms, sign_ms, post_ms, timing=timing,
                )
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID", side="BUY", price=market_price,
                    token_id=token_id[:16] + "...",
                    bal_ms=bal_ms, sign_ms=sign_ms, post_ms=post_ms,
                    submit_ack_ms=sign_ms + post_ms,
                )

            time.sleep(5)
            verified = self._verify_buy_via_balance(
                order_id, market_price, float(shares), token_id, balance_before,
            )
            verified.bal_ms = bal_ms
            verified.sign_ms = sign_ms
            verified.post_ms = post_ms
            verified.submit_ack_ms = sign_ms + post_ms
            self._log_order_latency(
                token_id, market_price, shares,
                "filled" if verified.success else "acked_unverified",
                bal_ms, sign_ms, post_ms, timing=timing,
            )
            return verified

        except Exception as e:
            if _is_fak_no_fill_error(e):
                self._log_order_latency(
                    token_id, market_price, shares, "no_fill_liquidity_gone",
                    bal_ms, sign_ms, post_ms, timing=timing,
                )
                return OrderResult(
                    success=False,
                    status=REJECTED,
                    error="fak_no_fill_liquidity_gone",
                    side="BUY",
                    price=market_price,
                    amount_usd=planned_spend,
                    shares=shares,
                    token_id=token_id[:16] + "...",
                    dry_run=False,
                    bal_ms=bal_ms, sign_ms=sign_ms, post_ms=post_ms,
                    submit_ack_ms=sign_ms + post_ms,
                )

            time.sleep(3)
            balance_after = self.get_collateral_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 1.0:
                actual_shares = float(int(shares))
                planned_notional = round(actual_shares * market_price, 2)
                estimated_fee = max(0.0, spent - planned_notional)
                print(f"  👻 GHOST BUY: balance dropped ${spent:.2f} despite error")
                self._log_order_latency(
                    token_id, market_price, shares, "ghost_filled",
                    bal_ms, sign_ms, post_ms, timing=timing,
                )
                return OrderResult(
                    success=True, order_id="ghost-buy",
                    status=FILLED, side="BUY", price=market_price,
                    amount_usd=spent, shares=actual_shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                    planned_order_notional_usd=planned_notional,
                    actual_cash_spent_usd=spent,
                    estimated_fee_usd=estimated_fee,
                    bal_ms=bal_ms, sign_ms=sign_ms, post_ms=post_ms,
                    submit_ack_ms=sign_ms + post_ms,
                )

            self._log_order_latency(
                token_id, market_price, shares, "error",
                bal_ms, sign_ms, post_ms, timing=timing,
            )
            return OrderResult(
                success=False, status=FAILED, error=sanitize_exception_text(e),
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
                bal_ms=bal_ms, sign_ms=sign_ms, post_ms=post_ms,
                submit_ack_ms=sign_ms + post_ms,
            )

    def _log_order_latency(
        self, token_id: str, price: float, shares: float, outcome: str,
        bal_ms: float, sign_ms: float, post_ms: float,
        timing: Optional[dict[str, Any]] = None,
    ) -> None:
        """Print + persist the submit->ack latency split for post-run analysis.

        post_ms is the network round-trip plus server-side FAK match (one bucket;
        the match happens inside the POST response, inseparable client-side). The
        no-fills are lost in this window, so this is the number to drive down.
        """
        submit_ack_ms = sign_ms + post_ms
        print(
            f"  ⏱  order latency: bal={bal_ms:.0f}ms sign={sign_ms:.0f}ms "
            f"post(net+match)={post_ms:.0f}ms | submit→ack={submit_ack_ms:.0f}ms "
            f"[{outcome}]"
        )
        try:
            timing = timing or {}
            log_dir = os.getenv("LOG_DIR", "logs")
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "order_latency.csv")
            new_file = not os.path.exists(path)
            fields = [
                "ts", "token_id", "price", "shares", "outcome",
                "bal_ms", "sign_ms", "post_ms", "submit_ack_ms",
                "signal_ready_ts", "final_book_snapshot_ts", "executor_buy_start_ts",
                "sign_start_ts", "sign_end_ts", "post_start_ts", "post_end_ts",
                "book_age_ms", "book_hash", "sdk_warmed",
            ]
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(fields)
                writer.writerow([
                    f"{time.time():.3f}", str(token_id), f"{price:.4f}",
                    f"{float(shares):.0f}", outcome,
                    f"{bal_ms:.1f}", f"{sign_ms:.1f}", f"{post_ms:.1f}",
                    f"{submit_ack_ms:.1f}",
                    _fmt_ts(timing.get("signal_ready_ts")),
                    _fmt_ts(timing.get("final_book_snapshot_ts")),
                    _fmt_ts(timing.get("executor_buy_start_ts")),
                    _fmt_ts(timing.get("sign_start_ts")),
                    _fmt_ts(timing.get("sign_end_ts")),
                    _fmt_ts(timing.get("post_start_ts")),
                    _fmt_ts(timing.get("post_end_ts")),
                    _fmt_float(timing.get("book_age_ms")),
                    str(timing.get("book_hash") or ""),
                    int(bool(timing.get("sdk_warmed"))),
                ])
        except Exception as e:
            print(f"  ⏱  latency log failed: {sanitize_exception_text(e)}")

    def _verify_buy_via_balance(
        self, order_id: str, price: float, shares: float,
        token_id: str, balance_before: float,
    ) -> OrderResult:
        """Verify buy fill without cancelling unresolved FAK orders."""
        for attempt in range(3):
            balance_after = self.get_collateral_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 0.50:
                actual_shares = float(int(shares))
                planned_notional = round(actual_shares * price, 2)
                estimated_fee = max(0.0, spent - planned_notional)
                suffix = f" (attempt {attempt+1})" if attempt > 0 else ""
                print(f"  ✓ Balance verified{suffix}: spent ${spent:.2f} "
                      f"({actual_shares:.0f} shares @ ${price:.3f}; "
                      f"fee≈${estimated_fee:.2f})")
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side="BUY", price=price,
                    amount_usd=spent, shares=actual_shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                    planned_order_notional_usd=planned_notional,
                    actual_cash_spent_usd=spent,
                    estimated_fee_usd=estimated_fee,
                )

            fill = self._check_order(order_id)
            if fill:
                matched = self._extract_fill(fill, price)
                if matched:
                    suffix = f" (attempt {attempt+1})" if attempt > 0 else ""
                    print(f"  ✓ Order API verified{suffix}: "
                          f"{matched[2]:.0f} shares @ ${matched[0]:.3f}")
                    return OrderResult(
                        success=True, order_id=order_id, status=FILLED,
                        side="BUY", price=matched[0],
                        amount_usd=matched[1], shares=matched[2],
                        token_id=token_id[:16] + "...", dry_run=False,
                        planned_order_notional_usd=round(matched[0] * matched[2], 2),
                        actual_cash_spent_usd=matched[1],
                        estimated_fee_usd=max(0.0, matched[1] - round(matched[0] * matched[2], 2)),
                    )

            if attempt < 2:
                time.sleep(3)

        print(f"  ⏳ Buy unverified after {3*3+5}s — NOT cancelling "
              f"(Polygon may still be settling)")
        return OrderResult(
            success=False, order_id=order_id, status=FAILED,
            error="UNVERIFIED_BUY",
            side="BUY", price=price, amount_usd=shares * price,
            shares=shares, token_id=token_id[:16] + "...",
        )

    def sell(self, token_id: str, shares: float, price: float = 0.0) -> OrderResult:
        """Sell shares and verify via collateral balance change."""
        sell_shares = int(shares)
        if sell_shares < 1:
            return OrderResult(
                success=False, status=REJECTED,
                error="Less than 1 share", side="SELL",
            )

        if self.dry_run:
            sim_price = price if price > 0 else 0.90
            revenue = sell_shares * sim_price
            return OrderResult(
                success=True, order_id=f"DRY-SELL-{int(time.time())}",
                status=FILLED, side="SELL", price=sim_price,
                amount_usd=revenue, shares=float(sell_shares),
                shares_remaining=0.0,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized or not self.client:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        if price <= 0:
            notional = float(sell_shares) * 0.50
            price = self.get_market_price(token_id, "SELL", notional)
            if price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get sell price", side="SELL",
                    token_id=token_id[:16] + "...",
                )

        if sell_shares < self.min_order_size:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Size {sell_shares:.0f} shares < {self.min_order_size:.0f} min "
                      f"— hold to resolution",
                side="SELL", price=price, shares=float(sell_shares),
                shares_remaining=float(sell_shares),
                token_id=token_id[:16] + "...",
            )

        sell_amount = round(sell_shares * price, 2)
        print(f"  📊 Sell: {sell_shares} shares @ ${price:.3f} = ${sell_amount:.2f}")

        balance_before = self.get_collateral_balance()

        try:
            order_args = OrderArgsV2(
                token_id=token_id,
                price=self._round_price_to_tick(price),
                size=float(int(sell_shares)),
                side="SELL",
                user_usdc_balance=balance_before,
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC, False)
            order_id = result.get("orderID", "")

            time.sleep(2)
            balance_after = self.get_collateral_balance()
            received = balance_after - balance_before

            if received > 0.10:
                shares_sold = received / price if price > 0 else 0
                shares_left = max(0, sell_shares - shares_sold)
                status = FILLED if shares_left < 1 else PARTIAL
                if status == PARTIAL:
                    print(f"  ⚠️  Partial fill: sold ~{shares_sold:.0f} of {sell_shares}, "
                          f"~{shares_left:.0f} remaining")
                return OrderResult(
                    success=True, order_id=order_id or "balance-verified",
                    status=status, side="SELL", price=price,
                    amount_usd=received, shares=shares_sold,
                    shares_remaining=shares_left,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            if order_id:
                fill = self._check_order(order_id)
                if fill:
                    matched = self._extract_fill(fill, price)
                    if matched:
                        return OrderResult(
                            success=True, order_id=order_id, status=FILLED,
                            side="SELL", price=matched[0],
                            amount_usd=matched[1], shares=matched[2],
                            shares_remaining=max(0, sell_shares - matched[2]),
                            token_id=token_id[:16] + "...", dry_run=False,
                        )
                self.cancel_order(order_id)

            return OrderResult(
                success=False, order_id=order_id or "", status=FAILED,
                error="Sell not verified (no balance change)",
                side="SELL", price=price, token_id=token_id[:16] + "...",
            )

        except Exception as e:
            time.sleep(1)
            balance_after = self.get_collateral_balance()
            received = balance_after - balance_before
            if received > 0.10:
                shares_sold = received / price if price > 0 else 0
                shares_left = max(0, sell_shares - shares_sold)
                print(f"  👻 Ghost sell! Got ${received:.2f} despite error")
                return OrderResult(
                    success=True, order_id="ghost-sell",
                    status=PARTIAL if shares_left >= 1 else FILLED,
                    side="SELL", price=price,
                    amount_usd=received, shares=shares_sold,
                    shares_remaining=shares_left,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            return OrderResult(
                success=False, status=FAILED, error=sanitize_exception_text(e),
                side="SELL", price=price, token_id=token_id[:16] + "...",
            )


    def _extract_fill(self, fill: dict, fallback_price: float) -> Optional[tuple]:
        size_matched = float(
            fill.get("size_matched", 0) if isinstance(fill, dict)
            else getattr(fill, "size_matched", 0)
        )
        if size_matched <= 0:
            return None
        fill_price = float(
            fill.get("price", fallback_price) if isinstance(fill, dict)
            else getattr(fill, "price", fallback_price)
        )
        return (fill_price, size_matched * fill_price, size_matched)

    def _check_order(self, order_id: str) -> Optional[dict]:
        if not self._initialized or not self.client:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            print(f"[executor] Order check failed: {sanitize_exception_text(e)}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run or not self._initialized or not self.client:
            return True
        try:
            self.client.cancel_orders([order_id])
            return True
        except Exception as e:
            print(f"[executor] Cancel failed: {sanitize_exception_text(e)}")
            return False

    def cancel_all(self) -> bool:
        if self.dry_run or not self._initialized or not self.client:
            return True
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"[executor] Cancel all failed: {sanitize_exception_text(e)}")
            return False
