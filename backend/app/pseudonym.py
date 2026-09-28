"""投稿者の仮名 ID（HMAC-SHA256）。ユーザー ID そのものは保存しない。"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

SALT_FILENAME = "pseudonym_salt"


def load_or_create_salt(data_dir: Path, env_salt: str | None = None) -> bytes:
    """環境変数のソルトがあればそれを、なければ data_dir のファイルを使う（なければ作る）。"""
    if env_salt:
        return env_salt.encode("utf-8")
    path = data_dir / SALT_FILENAME
    if path.exists():
        return path.read_text(encoding="utf-8").strip().encode("utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    path.write_text(salt + "\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return salt.encode("utf-8")


class Pseudonymizer:
    def __init__(self, salt: bytes) -> None:
        if not salt:
            raise ValueError("salt must not be empty")
        self._salt = salt

    def __call__(self, user_id: str) -> str:
        return hmac.new(self._salt, user_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
