"""Thin async client for the Yahoo Fantasy Sports v2 API."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import xmlutil
from .auth import TokenStore

BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"


class YahooAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Yahoo API error {status}: {_error_description(body)}")


def _error_description(body: str) -> str:
    try:
        data = xmlutil.parse(body)
        if isinstance(data, dict) and data.get("description"):
            return data["description"]
    except Exception:
        pass
    return body[:500]


class YahooClient:
    def __init__(self, tokens: TokenStore, http: httpx.AsyncClient | None = None):
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=30)

    async def _request(self, method: str, path: str, body: str | None = None) -> Any:
        for attempt in range(2):
            # Token refresh is a blocking HTTP call; keep it off the event loop.
            token = await asyncio.to_thread(self.tokens.access_token)
            headers = {"Authorization": f"Bearer {token}"}
            if body is not None:
                headers["Content-Type"] = "application/xml"
            resp = await self.http.request(method, path, content=body, headers=headers)
            if resp.status_code == 401 and attempt == 0:
                # Token revoked or expired early: refresh once and retry.
                await asyncio.to_thread(self.tokens.refresh)
                continue
            if resp.status_code >= 400:
                raise YahooAPIError(resp.status_code, resp.text)
            return xmlutil.parse(resp.text) if resp.text.strip() else {}
        raise AssertionError("unreachable")

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def put(self, path: str, body: dict) -> Any:
        return await self._request("PUT", path, xmlutil.build(body))

    async def post(self, path: str, body: dict) -> Any:
        return await self._request("POST", path, xmlutil.build(body))

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    async def aclose(self) -> None:
        await self.http.aclose()
