import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _int(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    app_name: str
    mode: str
    polymarket_host: str
    gamma_host: str
    anthropic_api_key: str
    claude_model: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    starting_bankroll: float
    target_bankroll: float
    risk_per_trade: float
    max_daily_loss: float
    max_drawdown: float
    max_open_exposure: float
    min_edge: float
    min_liquidity: float
    poll_seconds: int
    live_trading_enabled: bool
    db_path: Path

    @classmethod
    def from_environment(cls):
        mode = os.getenv("MODE", "PAPER").upper()

        if mode not in {"RESEARCH", "PAPER", "SHADOW", "LIVE"}:
            raise ValueError(
                "MODE must be RESEARCH, PAPER, SHADOW, or LIVE"
            )

        starting = _float("STARTING_BANKROLL", 1.0)
        target = _float("TARGET_BANKROLL", 3300000.0)

        if target <= starting:
            raise ValueError("TARGET_BANKROLL must exceed STARTING_BANKROLL")

        risk = _float("RISK_PER_TRADE", 0.01)
        daily = _float("MAX_DAILY_LOSS", 0.05)
        drawdown = _float("MAX_DRAWDOWN", 0.15)
        exposure = _float("MAX_OPEN_EXPOSURE", 0.25)

        for name, value in {
            "RISK_PER_TRADE": risk,
            "MAX_DAILY_LOSS": daily,
            "MAX_DRAWDOWN": drawdown,
            "MAX_OPEN_EXPOSURE": exposure,
        }.items():
            if value > 1:
                raise ValueError(f"{name} must be <= 1")

        db_value = os.getenv("DB_PATH", "data/friday.db")
        db_path = Path(db_value)

        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path

        return cls(
            app_name="FRIDAY",
            mode=mode,
            polymarket_host=os.getenv(
                "POLYMARKET_HOST",
                "https://clob.polymarket.com",
            ),
            gamma_host=os.getenv(
                "GAMMA_HOST",
                "https://gamma-api.polymarket.com",
            ),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5"),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
            elevenlabs_model=os.getenv(
                "ELEVENLABS_MODEL",
                "eleven_flash_v2_5",
            ),
            starting_bankroll=starting,
            target_bankroll=target,
            risk_per_trade=risk,
            max_daily_loss=daily,
            max_drawdown=drawdown,
            max_open_exposure=exposure,
            min_edge=_float("MIN_EDGE", 0.05),
            min_liquidity=_float("MIN_LIQUIDITY", 100),
            poll_seconds=_int("POLL_SECONDS", 10),
            live_trading_enabled=False,
            db_path=db_path,
        )


settings = Settings.from_environment()

# Backward-compatible names for Phase 0 modules.
APP_NAME = settings.app_name
MODE = settings.mode
POLYMARKET_HOST = settings.polymarket_host
GAMMA_HOST = settings.gamma_host
ANTHROPIC_API_KEY = settings.anthropic_api_key
CLAUDE_MODEL = settings.claude_model
ELEVENLABS_API_KEY = settings.elevenlabs_api_key
ELEVENLABS_VOICE_ID = settings.elevenlabs_voice_id
ELEVENLABS_MODEL = settings.elevenlabs_model
STARTING_BANKROLL = settings.starting_bankroll
TARGET_BANKROLL = settings.target_bankroll
RISK_PER_TRADE = settings.risk_per_trade
MAX_DAILY_LOSS = settings.max_daily_loss
MAX_DRAWDOWN = settings.max_drawdown
MAX_OPEN_EXPOSURE = settings.max_open_exposure
MIN_EDGE = settings.min_edge
MIN_LIQUIDITY = settings.min_liquidity
POLL_SECONDS = settings.poll_seconds

# PHASE 1 SAFETY INVARIANT:
# configuration can never enable actual order execution.
LIVE_TRADING_ENABLED = False

DB_PATH = str(settings.db_path)
