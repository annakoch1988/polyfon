"""Polymarket fee calculation per official docs.

Fee formula: fee = C * feeRate * p * (1 - p)
where C = number of shares traded, p = price of shares.
Taker only. Maker fee = 0.
Crypto feeRate = 0.07 (7%).
Fees rounded to 5 decimal places.
"""


def taker_fee_usdc(shares: float, price: float, fee_rate: float = 0.07) -> float:
    """Calculate taker fee in USDC.

    Args:
        shares: Number of shares traded.
        price: Share price (0 to 1).
        fee_rate: Market-specific fee rate (default 0.07 for crypto).

    Returns:
        Fee amount in USDC, rounded to 5 decimal places.
    """
    fee = shares * fee_rate * price * (1.0 - price)
    return round(fee, 5)


def effective_cost(shares: float, price: float, fee_rate: float = 0.07, is_taker: bool = True) -> float:
    """Total cost to buy shares including fee.

    Returns:
        Total USDC spent (price * shares + fee).
    """
    notional = shares * price
    if is_taker:
        fee = taker_fee_usdc(shares, price, fee_rate)
        return notional + fee
    return notional


def net_pnl(shares: float, entry_price: float, exit_price: float,
            entry_fee_rate: float = 0.07, exit_fee_rate: float = 0.07,
            is_taker: bool = True) -> float:
    """Net PnL for a position, accounting for fees on both legs.

    For a LONG_YES position: gross PnL = (exit - entry) * shares.
    Fees reduce PnL on both entry and exit.
    """
    gross = (exit_price - entry_price) * shares
    if is_taker:
        entry_fee = taker_fee_usdc(shares, entry_price, entry_fee_rate)
        exit_fee = taker_fee_usdc(shares, exit_price, exit_fee_rate)
        return gross - entry_fee - exit_fee
    return gross
