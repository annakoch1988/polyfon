"""Application configuration loaded from environment."""
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./polyfon.db"
    polymarket_api_url: str = "https://clob.polymarket.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    coins: str = "BTC,ETH"
    log_level: str = "INFO"
    binance_silence_threshold_sec: float = 5.0
    # How far ahead to discover/list upcoming windows (minutes)
    discovery_horizon_minutes: int = 720
    # Clock source: "system" or "binance"
    clock_source: str = "system"
    clock_sync_interval_sec: int = 60

    @property
    def coin_list(self) -> List[str]:
        return [c.strip().upper() for c in self.coins.split(",") if c.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
