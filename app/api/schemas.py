from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

VALID_TIMEFRAMES = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")


def _validate_timeframe(v: str) -> str:
    if v not in VALID_TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {sorted(VALID_TIMEFRAMES)}, got {v!r}")
    return v


def _validate_leverage(v: float) -> float:
    # Binance sets leverage as an integer; the strategy's own sizing/liquidation
    # math (StrategyParams.leverage) uses this same value, so a fractional
    # leverage here would silently diverge from what the exchange actually
    # applies once int(leverage) is sent to /fapi/v1/leverage.
    if v != int(v):
        raise ValueError("leverage must be a whole number (Binance sets leverage as an integer)")
    return v


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)
    testnet: bool = False
    max_account_exposure_pct: float | None = Field(None, gt=0, le=10_000)
    # Withdrawal alert - Telegram notification only, no auto-withdrawal.
    withdraw_alert_enabled: bool = False
    withdraw_alert_threshold: float | None = Field(None, gt=0)


class AccountUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    api_key: str | None = Field(None, min_length=1)
    api_secret: str | None = Field(None, min_length=1)
    testnet: bool | None = None
    max_account_exposure_pct: float | None = Field(None, gt=0, le=10_000)
    withdraw_alert_enabled: bool | None = None
    withdraw_alert_threshold: float | None = Field(None, gt=0)


# OKX timeframes this bot allows: only candles OKX publishes natively.
# Mapped to OKX's own bar codes in okx_futures.OKX_BAR_MAP.
OKX_VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


def _validate_okx_timeframe(v: str) -> str:
    if v not in OKX_VALID_TIMEFRAMES:
        raise ValueError(f"OKX timeframe must be one of {sorted(OKX_VALID_TIMEFRAMES)} "
                         f"(OKX has no 8h candle), got {v!r}")
    return v


class PairCreate(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=20)
    timeframe: str | None = None
    # ---- strategy settings: OPTIONAL here. Anything left out is filled from the
    # owner's CONFIRMED default settings (see app/default_settings.py). ----
    hma_length: int | None = Field(None, ge=1, le=500)
    ema_filter_length: int | None = Field(None, ge=1, le=2000)
    ema_sizing_length: int | None = Field(None, ge=1, le=2000)
    atr_length: int | None = Field(None, ge=1, le=500)
    use_hold_rule: bool | None = None
    adx_di_length: int | None = Field(None, ge=1, le=500)
    adx_smoothing: int | None = Field(None, ge=1, le=500)
    hold_adx_level: float | None = Field(None, ge=0, le=100)
    trend_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    counter_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    leverage: float | None = Field(None, ge=1, le=125)
    counter_leverage: float | None = Field(None, ge=1, le=125)
    isolated_margin: bool | None = None
    wait_first_reversal: bool | None = None
    stop_loss_pct_equity: float | None = Field(None, ge=0.1, le=100)
    tp1_atr_mult: float | None = Field(None, ge=0.1, le=100)
    tp2_atr_mult: float | None = Field(None, ge=0.1, le=100)
    use_shadow: bool | None = None
    shadow_trades: int | None = Field(None, ge=1, le=100)
    shadow_slippage_ticks: int | None = Field(None, ge=0, le=100000)
    tracker_start_balance: float | None = Field(None, gt=0, le=1e12)

    poll_seconds: int | None = Field(None, ge=5, le=3600)
    telegram_enabled: bool | None = None
    ws_staleness_seconds: float | None = Field(None, ge=5, le=600)

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        v = v.upper().strip()
        if not SYMBOL_RE.match(v):
            raise ValueError(f"symbol must be 3-20 uppercase letters/digits, got {v!r}")
        return v

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, v: str | None) -> str | None:
        return v if v is None else _validate_timeframe(v)

    @field_validator("leverage", "counter_leverage")
    @classmethod
    def _leverage(cls, v: float | None) -> float | None:
        return v if v is None else _validate_leverage(v)


class PairUpdate(BaseModel):
    timeframe: str | None = None
    hma_length: int | None = Field(None, ge=1, le=500)
    ema_filter_length: int | None = Field(None, ge=1, le=2000)
    ema_sizing_length: int | None = Field(None, ge=1, le=2000)
    atr_length: int | None = Field(None, ge=1, le=500)
    use_hold_rule: bool | None = None
    adx_di_length: int | None = Field(None, ge=1, le=500)
    adx_smoothing: int | None = Field(None, ge=1, le=500)
    hold_adx_level: float | None = Field(None, ge=0, le=100)
    trend_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    counter_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    leverage: float | None = Field(None, ge=1, le=125)
    counter_leverage: float | None = Field(None, ge=1, le=125)
    isolated_margin: bool | None = None
    wait_first_reversal: bool | None = None
    stop_loss_pct_equity: float | None = Field(None, ge=0.1, le=100)
    tp1_atr_mult: float | None = Field(None, ge=0.1, le=100)
    tp2_atr_mult: float | None = Field(None, ge=0.1, le=100)
    use_shadow: bool | None = None
    shadow_trades: int | None = Field(None, ge=1, le=100)
    shadow_slippage_ticks: int | None = Field(None, ge=0, le=100000)
    tracker_start_balance: float | None = Field(None, gt=0, le=1e12)
    poll_seconds: int | None = Field(None, ge=5, le=3600)
    telegram_enabled: bool | None = None
    ws_staleness_seconds: float | None = Field(None, ge=5, le=600)

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, v: str | None) -> str | None:
        return v if v is None else _validate_timeframe(v)

    @field_validator("leverage", "counter_leverage")
    @classmethod
    def _leverage(cls, v: float | None) -> float | None:
        return v if v is None else _validate_leverage(v)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=200)


class ForgotPasswordRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=200)


class WalkForwardRequest(BaseModel):
    # Every number is entered on the dashboard - there are no defaults.
    # SIMULATION-ONLY: this analysis endpoint never touches live trading.
    n_folds: int = Field(..., ge=2, le=20)
    limit: int = Field(..., ge=200, le=5000)  # how many historical candles to pull for the analysis
    initial_equity: float = Field(..., gt=0, le=1e12)
    commission_pct: float = Field(..., ge=0, le=5)
    slippage_pct: float = Field(..., ge=0, le=5)


class MonteCarloRequest(BaseModel):
    n_sims: int = Field(..., ge=10, le=20_000)
    limit: int = Field(..., ge=200, le=5000)
    initial_equity: float = Field(..., gt=0, le=1e12)
    seed: int | None = None
    commission_pct: float = Field(..., ge=0, le=5)
    slippage_pct: float = Field(..., ge=0, le=5)


# ============================================================================
# OKX tab schemas - deliberately separate from the Binance schemas above, per
# the owner's decision that each platform's tab only asks for what THAT
# platform actually requires. Strategy fields below are IDENTICAL in name
# and type to the Binance PairCreate/PairUpdate ones above - that part is
# shared strategy, not a platform requirement, and must stay in lockstep. Only the platform-specific fields (symbol format, passphrase,
# demo vs testnet, sub-account label, trigger price type) differ.
# ============================================================================

OKX_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}-[A-Z0-9]{2,10}-SWAP$")  # e.g. BTC-USDT-SWAP


class OKXAccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)
    passphrase: str = Field(..., min_length=1)   # OKX-only third credential - no Binance equivalent
    demo: bool = False                            # OKX's x-simulated-trading flag
    sub_account_label: str = Field("", max_length=80)  # informational only - see okx_store.py
    max_account_exposure_pct: float | None = Field(None, gt=0, le=10_000)
    withdraw_alert_enabled: bool = False
    withdraw_alert_threshold: float | None = Field(None, gt=0)


class OKXAccountUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    api_key: str | None = Field(None, min_length=1)
    api_secret: str | None = Field(None, min_length=1)
    passphrase: str | None = Field(None, min_length=1)
    demo: bool | None = None
    sub_account_label: str | None = Field(None, max_length=80)
    max_account_exposure_pct: float | None = Field(None, gt=0, le=10_000)
    withdraw_alert_enabled: bool | None = None
    withdraw_alert_threshold: float | None = Field(None, gt=0)


class OKXPairCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=40)
    timeframe: str | None = None
    # ---- strategy settings: OPTIONAL here. Anything left out is filled from the
    # owner's CONFIRMED default settings (see app/default_settings.py). ----
    hma_length: int | None = Field(None, ge=1, le=500)
    ema_filter_length: int | None = Field(None, ge=1, le=2000)
    ema_sizing_length: int | None = Field(None, ge=1, le=2000)
    atr_length: int | None = Field(None, ge=1, le=500)
    use_hold_rule: bool | None = None
    adx_di_length: int | None = Field(None, ge=1, le=500)
    adx_smoothing: int | None = Field(None, ge=1, le=500)
    hold_adx_level: float | None = Field(None, ge=0, le=100)
    trend_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    counter_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    leverage: float | None = Field(None, ge=1, le=125)
    counter_leverage: float | None = Field(None, ge=1, le=125)
    isolated_margin: bool | None = None
    wait_first_reversal: bool | None = None
    stop_loss_pct_equity: float | None = Field(None, ge=0.1, le=100)
    tp1_atr_mult: float | None = Field(None, ge=0.1, le=100)
    tp2_atr_mult: float | None = Field(None, ge=0.1, le=100)
    use_shadow: bool | None = None
    shadow_trades: int | None = Field(None, ge=1, le=100)
    shadow_slippage_ticks: int | None = Field(None, ge=0, le=100000)
    tracker_start_balance: float | None = Field(None, gt=0, le=1e12)

    poll_seconds: int | None = Field(None, ge=5, le=3600)
    telegram_enabled: bool | None = None
    ws_staleness_seconds: float | None = Field(None, ge=5, le=600)
    trigger_px_type: str | None = Field(None, pattern="^(mark|last|index)$")

    @field_validator("symbol")
    @classmethod
    def _okx_symbol(cls, v: str) -> str:
        v = v.upper().strip()
        if not OKX_SYMBOL_RE.match(v):
            raise ValueError(f"symbol must be OKX SWAP instId format, e.g. 'BTC-USDT-SWAP', got {v!r}")
        return v

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, v: str | None) -> str | None:
        return v if v is None else _validate_okx_timeframe(v)

    @field_validator("leverage", "counter_leverage")
    @classmethod
    def _leverage(cls, v: float | None) -> float | None:
        return v if v is None else _validate_leverage(v)


class OKXPairUpdate(BaseModel):
    timeframe: str | None = None
    hma_length: int | None = Field(None, ge=1, le=500)
    ema_filter_length: int | None = Field(None, ge=1, le=2000)
    ema_sizing_length: int | None = Field(None, ge=1, le=2000)
    atr_length: int | None = Field(None, ge=1, le=500)
    use_hold_rule: bool | None = None
    adx_di_length: int | None = Field(None, ge=1, le=500)
    adx_smoothing: int | None = Field(None, ge=1, le=500)
    hold_adx_level: float | None = Field(None, ge=0, le=100)
    trend_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    counter_alloc_pct: float | None = Field(None, ge=0.1, le=100)
    leverage: float | None = Field(None, ge=1, le=125)
    counter_leverage: float | None = Field(None, ge=1, le=125)
    isolated_margin: bool | None = None
    wait_first_reversal: bool | None = None
    stop_loss_pct_equity: float | None = Field(None, ge=0.1, le=100)
    tp1_atr_mult: float | None = Field(None, ge=0.1, le=100)
    tp2_atr_mult: float | None = Field(None, ge=0.1, le=100)
    use_shadow: bool | None = None
    shadow_trades: int | None = Field(None, ge=1, le=100)
    shadow_slippage_ticks: int | None = Field(None, ge=0, le=100000)
    tracker_start_balance: float | None = Field(None, gt=0, le=1e12)
    poll_seconds: int | None = Field(None, ge=5, le=3600)
    telegram_enabled: bool | None = None
    ws_staleness_seconds: float | None = Field(None, ge=5, le=600)
    trigger_px_type: str | None = Field(None, pattern="^(mark|last|index)$")

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, v: str | None) -> str | None:
        return v if v is None else _validate_okx_timeframe(v)

    @field_validator("leverage", "counter_leverage")
    @classmethod
    def _leverage(cls, v: float | None) -> float | None:
        return v if v is None else _validate_leverage(v)




# ---- Default Settings page (see app/default_settings.py) ----
class DefaultSettingsSave(BaseModel):
    # field name -> value; a blank / null value clears that field
    values: dict


class AlertEmailSave(BaseModel):
    email: str = Field("", max_length=254)


class LiveTradingSwitch(BaseModel):
    enabled: bool
