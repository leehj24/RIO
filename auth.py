import os
import time
import json
from pathlib import Path
from typing import Optional, Dict, Any

import requests


class TossAuthError(RuntimeError):
    pass


class TossTokenManager:
    """
    Toss OAuth2 Client Credentials token manager.

    핵심:
    - access token을 매번 수동으로 .env에 넣지 않는다.
    - token cache 파일을 보고 만료 전이면 재사용한다.
    - 없거나 만료됐으면 client_id/client_secret으로 새로 발급한다.
    - 옵션으로 .env의 TOSS_ACCESS_TOKEN 값을 자동 갱신한다.

    기본 token endpoint:
    https://openapi.tossinvest.com/oauth2/token

    실제 Toss 문서의 token endpoint가 다르면 .env의 TOSS_TOKEN_URL만 바꾸면 된다.
    """

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        token_url: Optional[str] = None,
        cache_path: Optional[str] = None,
        env_path: str = ".env",
        write_token_to_env: bool = False,
        early_refresh_seconds: int = 300,
    ):
        self.client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        self.token_url = token_url or os.getenv("TOSS_TOKEN_URL", "https://openapi.tossinvest.com/oauth2/token")
        self.cache_path = Path(cache_path or os.getenv("TOSS_TOKEN_CACHE_PATH", ".toss_token_cache.json"))
        self.env_path = Path(env_path or os.getenv("ENV_PATH", ".env"))
        self.write_token_to_env = write_token_to_env
        self.early_refresh_seconds = early_refresh_seconds

    def _load_cache(self) -> Dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self, data: Dict[str, Any]) -> None:
        self.cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _cache_valid(self, data: Dict[str, Any]) -> bool:
        token = data.get("access_token")
        expires_at = float(data.get("expires_at", 0))
        return bool(token) and time.time() < (expires_at - self.early_refresh_seconds)

    def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._load_cache()
            if self._cache_valid(cached):
                return cached["access_token"]

            env_token = os.getenv("TOSS_ACCESS_TOKEN", "")
            env_expires_at = float(os.getenv("TOSS_ACCESS_TOKEN_EXPIRES_AT", "0") or 0)
            if env_token and time.time() < (env_expires_at - self.early_refresh_seconds):
                return env_token

        return self.refresh_access_token()

    def refresh_access_token(self) -> str:
        if not self.client_id or not self.client_secret:
            # fallback: 이미 env에 token만 수동으로 넣은 경우
            env_token = os.getenv("TOSS_ACCESS_TOKEN", "")
            if env_token:
                return env_token
            raise TossAuthError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET missing. Cannot issue Toss access token.")

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # Some environments advertise zstd automatically when the optional
            # zstandard package is installed.  The token endpoint's streamed zstd
            # response can then fail to decode in urllib3, so request the broadly
            # supported encodings used by the main Toss API client instead.
            "Accept-Encoding": "gzip, deflate",
        }
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            resp = requests.post(self.token_url, headers=headers, data=data, timeout=30)
        except requests.RequestException as exc:
            # Includes ContentDecodingError.  Converting it keeps Flask's
            # TossAuthError handler in control instead of exposing a 500 traceback.
            raise TossAuthError(f"Toss token issue request failed: {exc}") from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise TossAuthError(f"Toss token issue failed {resp.status_code}: {detail}")

        body = resp.json()
        token = body.get("access_token") or body.get("accessToken")
        if not token:
            raise TossAuthError(f"Toss token response has no access_token: {body}")

        expires_in = int(body.get("expires_in") or body.get("expiresIn") or 86400)
        expires_at = time.time() + expires_in

        cache = {
            "access_token": token,
            "expires_in": expires_in,
            "expires_at": expires_at,
            "issued_at": time.time(),
            "token_type": body.get("token_type") or body.get("tokenType") or "Bearer",
        }
        self._save_cache(cache)

        if self.write_token_to_env:
            self.update_env_token(token, expires_at)

        return token

    def update_env_token(self, token: str, expires_at: float) -> None:
        """
        .env 파일의 TOSS_ACCESS_TOKEN, TOSS_ACCESS_TOKEN_EXPIRES_AT 값을 갱신한다.
        기존 .env가 없으면 생성한다.
        """
        lines = []
        if self.env_path.exists():
            lines = self.env_path.read_text(encoding="utf-8").splitlines()

        def upsert(key: str, value: str):
            nonlocal lines
            found = False
            new_lines = []
            for line in lines:
                if line.startswith(key + "="):
                    new_lines.append(f"{key}={value}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"{key}={value}")
            lines = new_lines

        upsert("TOSS_ACCESS_TOKEN", token)
        upsert("TOSS_ACCESS_TOKEN_EXPIRES_AT", str(int(expires_at)))

        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
