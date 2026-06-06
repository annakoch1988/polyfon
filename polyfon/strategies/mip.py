"""Strategy: Market Maker Inventory Pressure Exploitation (MIP)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class MIPStrategy(BaseStrategy):
    number = 12
    """Market Maker Inventory Pressure Exploitation.

    Infers aggregate market-maker inventory skew from the joint behaviour
    of the UP and DOWN token order books.  When size asymmetries persist in
    a direction the MM counterparty is accumulating inventory that must
    eventually be shed — and the direction of the forced quote migration
    can be predicted.

    Avellaneda-Stoikov:  delta_p = -gamma * sigma^2 * Q * tau
      - Over-long YES  → MM lowers both bid and ask → mid drops → BUY_NO.
      - Over-short YES → MM raises both bid and ask → mid rises → BUY_YES.

    Because the system does not currently track per-entity inventory or L2
    depth, we use a composite *inventory-pressure index* that jointly
    examines UP and DOWN token books:

        up_imb   = (up_bid - up_ask) / (up_bid + up_ask)
        down_imb = (down_bid - down_ask) / (down_bid + down_ask)

        ip = up_imb - down_imb

    Interpretation:
      ip > 0 → strong YES buying + weak NO buying → MM short YES → BUY_YES.
      ip < 0 → strong NO buying + weak YES buying → MM long YES  → BUY_NO.

    This is complementary to OBI (which uses only the UP token); MIP
    explicitly models the *net* inventory exposure flowing through the MM's
    books and targets entry windows where MM quote adjustment is imminent
    (tau_max = 120s, tau_min = 60s per the prototype spec).

    Parameters (per MIP specification):
        ip_threshold:  Min |inventory pressure| to trade (default 0.30).
                       Mirrors the MIP spec IP_threshold range [0.3, 0.7].
        theta_sat:     |ip| at which confidence saturates (default 0.60).
        tau_max:       Latest entry before resolution (default 120s).
        tau_min:       Earliest entry — don't trade too close to expiry
                       when MMs withdraw (default 60s).
        q_max:         Position size.  *market* class = USDC to spend;
                       *limit* class = number of shares.
        theta_edge:    Minimum expected edge per share to trade.
        order_class:   'market' (default) or 'limit'.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                       Market -> FOK or FAK.  Limit -> GTC or GTD.
        fee_rate:      Market taker fee rate.
    """

    name = "MIP"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        ip_threshold: float = 0.30,
        theta_sat: float = 0.60,
        tau_max: float = 120.0,
        tau_min: float = 60.0,
        replay_cadence_seconds: float = 1.0,
        q_max: float = 1.0,
        theta_edge: float = 0.01,
        order_class: str = "market",
        time_in_force: str = "FOK",
        fee_rate: float = 0.07,
    ):
        if order_class not in ("market", "limit"):
            raise ValueError(f"order_class must be 'market' or 'limit', got {order_class!r}")
        valid_tif = self._MARKET_TIF if order_class == "market" else self._LIMIT_TIF
        if time_in_force not in valid_tif:
            raise ValueError(
                f"time_in_force {time_in_force!r} invalid for {order_class} orders. "
                f"Use {sorted(valid_tif)}"
            )
        self.ip_threshold = ip_threshold
        self.theta_sat = theta_sat
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.q_max = q_max
        self.theta_edge = theta_edge
        self.order_class = order_class
        self.time_in_force = time_in_force
        self.fee_rate = fee_rate

    def _resolve_order(self, price: float) -> tuple[float, float] | None:
        """Return (shares, notional_usdc) or None if constraints fail."""
        if self.order_class == "market":
            usdc = self.q_max
            if usdc < 1.0:
                return None
            shares = usdc / price
        else:
            shares = self.q_max
            if shares < 5 or shares * price < 1.0:
                return None
        return shares, shares * price

    def _inventory_pressure(self, context: Context) -> Optional[float]:
        """Compute the composite inventory-pressure index.

        Uses both UP and DOWN token books to infer the net flow that the
        MM has absorbed.  Returns None if book data is insufficient.
        """
        up_bs = context.up_bid_size
        up_as = context.up_ask_size
        down_bs = context.down_bid_size
        down_as = context.down_ask_size

        # Must have both tokens' books available.
        if up_bs is None or up_as is None or down_bs is None or down_as is None:
            return None
        up_total = up_bs + up_as
        down_total = down_bs + down_as
        if up_total <= 0 or down_total <= 0:
            return None

        up_imb = (up_bs - up_as) / up_total
        down_imb = (down_bs - down_as) / down_total

        # Net pressure: UP buying minus DOWN buying.
        #   ip > 0 → YES side has heavy buying pressure → MM short YES → BUY_YES.
        #   ip < 0 → NO  side has heavy buying pressure → MM long YES  → BUY_NO.
        return up_imb - down_imb

    def on_tick(self, window: Any, context: Context) -> Optional[Signal]:
        if context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        ip = self._inventory_pressure(context)
        if ip is None:
            return None
        if abs(ip) < self.ip_threshold:
            return None

        # UP buying dominates → BUY_YES.
        if ip > self.ip_threshold and context.up_best_ask is not None:
            price = context.up_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(ip / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "inventory_pressure": ip,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                        "notional": notional,
                    },
                )

        # NO buying dominates → BUY_NO.
        if ip < -self.ip_threshold and context.down_best_ask is not None:
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(-ip / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "inventory_pressure": ip,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                        "notional": notional,
                    },
                )

        return None

    def build_replay_plan(self, window: Any) -> ReplayPlan:
        start = window.end_et - timedelta(seconds=self.tau_max)
        end = window.end_et - timedelta(seconds=self.tau_min)
        if end < start:
            return ReplayPlan(eval_times=[])
        if self.replay_cadence_seconds <= 0:
            raise ValueError("replay_cadence_seconds must be > 0")
        eval_times = []
        current = start
        step = timedelta(seconds=self.replay_cadence_seconds)
        while current <= end:
            eval_times.append(current)
            current += step
        return ReplayPlan(eval_times=eval_times, stop_on_signal=True, cadence_seconds=self.replay_cadence_seconds)

    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        return None
