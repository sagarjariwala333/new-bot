"""
_test_values.py
===============

TEST-ONLY placeholder settings. These are arbitrary numbers chosen purely so
the infrastructure tests can exercise code paths that need *some* value.

They are NOT the owner's settings, are not recommendations, and are not used
anywhere outside the tests. The product itself ships with every setting blank
(see app/default_settings.py).
"""

from app import strategy as strat
from app.store import PairConfig
from app.okx_store import OKXPairConfig

STRATEGY_VALUES = dict(
    hma_length=11, ema_filter_length=23, ema_sizing_length=47, atr_length=9,
    use_hold_rule=True, adx_di_length=10, adx_smoothing=12, hold_adx_level=21.0,
    trend_alloc_pct=12.0, counter_alloc_pct=4.0, leverage=7.0, counter_leverage=3.0,
    stop_loss_pct_equity=4.0, tp1_atr_mult=1.5, tp2_atr_mult=2.5,
    use_shadow=True, shadow_trades=4, shadow_slippage_ticks=10,
)

PAIR_EXTRA = dict(
    isolated_margin=True, wait_first_reversal=True, tracker_start_balance=5000.0,
    poll_seconds=20, telegram_enabled=True, ws_staleness_seconds=45.0, timeframe="1h",
)


def params(**over) -> strat.StrategyParams:
    return strat.StrategyParams(**{**STRATEGY_VALUES, **over})


def pair_config(symbol="TESTUSDT", **over) -> PairConfig:
    return PairConfig(symbol=symbol, **{**STRATEGY_VALUES, **PAIR_EXTRA, **over})


def okx_pair_config(symbol="TEST-USDT-SWAP", **over) -> OKXPairConfig:
    return OKXPairConfig(symbol=symbol, **{**STRATEGY_VALUES, **PAIR_EXTRA, "trigger_px_type": "mark", **over})


# Offline-analysis inputs (simulation only) - arbitrary test numbers.
ANALYSIS = dict(initial_equity=3000.0, commission_pct=0.05, slippage_pct=0.03)
