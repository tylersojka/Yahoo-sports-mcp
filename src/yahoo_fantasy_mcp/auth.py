"""Yahoo OAuth 2.0: one-time interactive login plus automatic token refresh."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
# "oob" makes Yahoo show the code on screen instead of redirecting,
# so no local web server or HTTPS certificate is needed.
DEFAULT_REDIRECT_URI = "oob"
# Refresh a little before Yahoo's expiry so a request never races it.
EXPIRY_MARGIN_SECONDS = 60


def token_path() -> Path:
    override = os.environ.get("YAHOO_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "yahoo-fantasy-mcp" / "token.json"


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI

    @classmethod
    def from_env(cls) -> "Credentials":
        client_id = os.environ.get("YAHOO_CLIENT_ID")
        client_secret = os.environ.get("YAHOO_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET must be set. "
                "Create an app at https://developer.yahoo.com/apps/create/ "
                "with Fantasy Sports Read/Write permission."
            )
        return cls(
            client_id,
            client_secret,
            os.environ.get("YAHOO_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        )


class TokenStore:
    """Holds the OAuth token on disk and refreshes it when it expires."""

    def __init__(self, creds: Credentials, path: Path | None = None):
        self.creds = creds
        self.path = path or token_path()
        self._token: dict | None = None

    def _load(self) -> dict:
        if self._token is None:
            if not self.path.exists():
                raise RuntimeError(
                    f"No Yahoo token at {self.path}. Run `yahoo-fantasy-mcp auth` first."
                )
            self._token = json.loads(self.path.read_text())
        return self._token

    def save(self, token: dict) -> None:
        token = dict(token)
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token, indent=2))
        self.path.chmod(0o600)
        self._token = token

    def _request_token(self, data: dict) -> dict:
        resp = httpx.post(
            TOKEN_URL,
            data=data,
            auth=(self.creds.client_id, self.creds.client_secret),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Yahoo token request failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def exchange_code(self, code: str) -> None:
        self.save(
            self._request_token(
                {
                    "grant_type": "authorization_code",
                    "code": code.strip(),
                    "redirect_uri": self.creds.redirect_uri,
                }
            )
        )

    def refresh(self) -> None:
        old = self._load()
        new = self._request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": old["refresh_token"],
                "redirect_uri": self.creds.redirect_uri,
            }
        )
        # Yahoo usually returns the same refresh token; keep the old one if omitted.
        new.setdefault("refresh_token", old["refresh_token"])
        self.save(new)

    def access_token(self) -> str:
        token = self._load()
        if time.time() >= token.get("expires_at", 0) - EXPIRY_MARGIN_SECONDS:
            self.refresh()
            token = self._load()
        return token["access_token"]


def authorize_url(creds: Credentials) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": creds.client_id,
            "redirect_uri": creds.redirect_uri,
            "response_type": "code",
            "language": "en-us",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def interactive_login() -> None:
    creds = Credentials.from_env()
    store = TokenStore(creds)
    print("1. Open this URL and approve access:\n")
    print(f"   {authorize_url(creds)}\n")
    print("2. Paste the code Yahoo shows you (or the `code=` value from the redirect URL).")
    code = input("\nCode: ").strip()
    if code.startswith("http"):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        code = params.get("code", [""])[0]
    store.exchange_code(code)
    print(f"\nSaved token to {store.path}. You're ready to run the server.")
