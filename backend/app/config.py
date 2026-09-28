"""設定の読み込み（.env と環境変数）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "config" / "defaults.yaml"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    pseudonym_salt: str | None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    discord_webhook_url: str | None = None


def load_settings(env_file: Path | None = None) -> Settings:
    """リポジトリ直下の .env を読み（環境変数が優先）、設定を返す。"""
    load_dotenv(env_file or REPO_ROOT / ".env", override=False)
    data_dir = Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
    db_path = Path(os.environ.get("DB_PATH") or data_dir / "moderation.db")
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        pseudonym_salt=os.environ.get("PSEUDONYM_SALT") or None,
        twitch_client_id=os.environ.get("TWITCH_CLIENT_ID") or None,
        twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET") or None,
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or None,
    )


def load_defaults(path: Path = DEFAULTS_PATH) -> dict[str, Any]:
    """config/defaults.yaml を読む。"""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
