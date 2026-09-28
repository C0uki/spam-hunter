"""設定の読み込み（.env と環境変数）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    pseudonym_salt: str | None


def load_settings(env_file: Path | None = None) -> Settings:
    """リポジトリ直下の .env を読み（環境変数が優先）、設定を返す。"""
    load_dotenv(env_file or REPO_ROOT / ".env", override=False)
    data_dir = Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
    db_path = Path(os.environ.get("DB_PATH") or data_dir / "moderation.db")
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        pseudonym_salt=os.environ.get("PSEUDONYM_SALT") or None,
    )
