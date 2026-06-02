"""Polymarket CLOB order execution.

Live entries use marketable FAK BUY limit orders with integer share size. This
keeps taker execution while avoiding the py-clob-client-v2 BUY market helper's
amount/price float division path.
"""

import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgsV2,
    MarketOrderArgsV2,
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
POLY_MIN_NOTIONAL = 5.0


@dataclass
class MarketMetadata:
    minimum_order_size: float = POLY_MIN_NOTIONAL
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


def _decimal_from_float(value: float | int | str) -> Decimal:
    """Convert numeric inputs through str() so 0.59 stays Decimal('0.59')."""
    return Decimal(str(value))


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
        self.min_order_size = POLY_MIN_NOTIONAL
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
            mos = float(info.get("mos") or POLY_MIN_NOTIONAL)
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

    def get_fee_rate_bps(self, token_id: str) -> float:
        """Return CLOB v2 fee rate in basis points for token metadata."""
        if not self._initialized or not self.client or not token_id:
            return 0.0
        try:
            return float(self.client.get_fee_rate_bps(token_id))
        except Exception as e:
            print(f"[executor] Fee metadata check failed: {sanitize_exception_text(e)}")
            return 0.0

    def buy(self, token_id: str, amount_usd: float, price: float = 0.0,
            balance_hint: float = -1.0) -> OrderResult:
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

        if self.dry_run:
            sim_price = round(float(price), 2) if price > 0 else 0.55
            return OrderResult(
                success=True, order_id=f"DRY-{int(time.time())}",
                status=FILLED, side="BUY", price=sim_price,
                amount_usd=amount_usd, shares=amount_usd / sim_price,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized or not self.client:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        if price > 0:
            market_price = self._round_price_to_tick(price)
        else:
            market_price = self.get_market_price(token_id, "BUY", amount_usd)
            if market_price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get market price", side="BUY",
                    token_id=token_id[:16] + "...",
                )
            market_price = self._round_price_to_tick(market_price)

        # Price cap: don't buy above MAX_BUY_PRICE
        if market_price > MAX_BUY_PRICE:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Price ${market_price:.3f} > cap ${MAX_BUY_PRICE:.2f}",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        shares, planned_spend = calculate_order_size(market_price, amount_usd)
        if shares < 1 or planned_spend <= 0:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Can't afford 1 share at ${market_price:.3f} "
                      f"within ${amount_usd:.2f}",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        if shares < self.min_order_size:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Size {shares:.0f} shares < {self.min_order_size:.0f} min",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        print(f"  📊 Market price: ${market_price:.3f}/share "
              f"→ {int(shares)} shares for ${planned_spend:.2f}")

        # Hot-path latency: get_collateral_balance() is a fresh ~250ms CLOB GET
        # on every order, in series before signing. With a caller hint (window-
        # open on-chain balance) we skip it, cutting book-read -> order-land
        # latency ~in half so the ask walks less before our FAK lands. The hint
        # is the window high-water balance (>= actual at submit, since balance
        # only drops on this buy), so the SDK fee-buffer never over-shrinks; the
        # post-order verify + window-boundary sync remain the source of truth.
        if balance_hint > 0:
            balance_before = balance_hint
        else:
            balance_before = self.get_collateral_balance()

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
                signed_order = self.client.create_order(order_args)
                result = self.client.post_order(signed_order, clob_order_type, False)
            except Exception as post_exc:
                # Direct placement blocked by Cloudflare (403) — activate Tor
                # for THIS order and re-submit once. Any other error (incl. FAK
                # no-fill 400) falls through to the outer handler unchanged.
                if _is_geoblock_403(post_exc) and self._activate_tor_fallback():
                    print("  🔁 Re-submitting this order via Tor (CF 403)...")
                    signed_order = self.client.create_order(order_args)
                    result = self.client.post_order(signed_order, clob_order_type, False)
                else:
                    raise

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID", side="BUY", price=market_price,
                    token_id=token_id[:16] + "...",
                )

            time.sleep(5)
            return self._verify_buy_via_balance(
                order_id, market_price, float(shares), token_id, balance_before,
            )

        except Exception as e:
            if _is_fak_no_fill_error(e):
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
                )

            time.sleep(3)
            balance_after = self.get_collateral_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 1.0:
                actual_shares = spent / market_price if market_price > 0 else 0
                print(f"  👻 GHOST BUY: balance dropped ${spent:.2f} despite error")
                return OrderResult(
                    success=True, order_id="ghost-buy",
                    status=FILLED, side="BUY", price=market_price,
                    amount_usd=spent, shares=actual_shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            return OrderResult(
                success=False, status=FAILED, error=sanitize_exception_text(e),
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

    def _verify_buy_via_balance(
        self, order_id: str, price: float, shares: float,
        token_id: str, balance_before: float,
    ) -> OrderResult:
        """Verify buy fill without cancelling unresolved FAK orders."""
        for attempt in range(3):
            balance_after = self.get_collateral_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 0.50:
                actual_shares = spent / price if price > 0 else shares
                suffix = f" (attempt {attempt+1})" if attempt > 0 else ""
                print(f"  ✓ Balance verified{suffix}: spent ${spent:.2f} "
                      f"(~{actual_shares:.0f} shares @ ${price:.3f})")
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side="BUY", price=price,
                    amount_usd=spent, shares=actual_shares,
                    token_id=token_id[:16] + "...", dry_run=False,
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
            order_args = MarketOrderArgsV2(
                token_id=token_id,
                amount=float(sell_shares),
                side="SELL",
                price=price,
                order_type=OrderType.GTC,
                user_usdc_balance=balance_before,
            )

            signed_order = self.client.create_market_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
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
