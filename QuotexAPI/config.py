"""Configuration management for QuotexAPI."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings


class QuotexConfig(BaseSettings):
    """Configuration settings for QuotexAPI."""

    # Authentication
    email: Optional[str] = Field(None, validation_alias="QUOTEX_EMAIL")
    password: Optional[str] = Field(None, validation_alias="QUOTEX_PASSWORD")
    ssid: Optional[str] = Field(None, validation_alias="QUOTEX_SSID")
    
    # Account Settings
    is_demo: bool = Field(default=True, validation_alias="QUOTEX_IS_DEMO")  # Default to demo
    tournament_id: int = Field(default=0, validation_alias="QUOTEX_TOURNAMENT_ID")

    # API Configuration
    api_url: str = Field(
        default="https://api.quotex.io", validation_alias="QUOTEX_API_URL"
    )
    ws_url: str = Field(
        default="wss://ws2.quotex.com/socket.io/?EIO=3&transport=websocket",
        validation_alias="QUOTEX_WS_URL"
    )

    # Cloudflare clearance copied from a logged-in browser (optional)
    cf_clearance: Optional[str] = Field(None, validation_alias="QUOTEX_CF_CLEARANCE")
    user_agent: Optional[str] = Field(None, validation_alias="QUOTEX_USER_AGENT")

    # Connection Settings
    reconnect_enabled: bool = Field(
        default=True, validation_alias="QUOTEX_RECONNECT_ENABLED"
    )
    max_reconnect_attempts: int = Field(
        default=5, validation_alias="QUOTEX_MAX_RECONNECT_ATTEMPTS"
    )
    reconnect_delay: float = Field(
        default=5.0, validation_alias="QUOTEX_RECONNECT_DELAY"
    )
    request_timeout: float = Field(
        default=30.0, validation_alias="QUOTEX_REQUEST_TIMEOUT"
    )

    # Logging
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


def load_config(env_file: Optional[Path] = None) -> QuotexConfig:
    """
    Load configuration from environment variables and .env file.

    Args:
        env_file: Path to .env file. If None, searches in current directory.

    Returns:
        QuotexConfig: Configuration instance.
    """
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    return QuotexConfig()


def get_config_from_dict(**kwargs) -> QuotexConfig:
    """
    Create configuration from dictionary.

    Args:
        **kwargs: Configuration parameters.

    Returns:
        QuotexConfig: Configuration instance.
    """
    return QuotexConfig(**kwargs)
