r"""Blizzard API access: OAuth, rate limiting, retries.

This is the only place that talks HTTP to Blizzard. It knows nothing about
characters, realms or collections: it authenticates, respects the published
limits, retries what is retryable and returns the parsed body together with the
exact bytes and the rate limit headers.

Everything above it (acquisition, storage, reporting) depends on this and never
on a transport detail.

Copiado de ``C:\collectorproject\wow_blizzard_client.py`` -- a mesma coisa que
já falou com a API em produção, sem uma linha mudada.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Self

try:
    dotenv_module = importlib.import_module("dotenv")
except ModuleNotFoundError:
    pass
else:
    dotenv_module.load_dotenv()

aiohttp: Any = None
with contextlib.suppress(ModuleNotFoundError):
    aiohttp = importlib.import_module("aiohttp")


BLIZZARD_CLIENT_ID = os.getenv("BLIZZARD_CLIENT_ID", "")
BLIZZARD_CLIENT_SECRET = os.getenv("BLIZZARD_CLIENT_SECRET", "")


@dataclass(frozen=True)
class RegionConfig:
    region: str
    token_url: str
    api_url: str
    namespace: str


class BlizzardJsonClient(Protocol):
    namespace: str
    locale: str
    region_config: RegionConfig

    async def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


REGION_CONFIGS = {
    "us": RegionConfig(
        "us",
        "https://us.battle.net/oauth/token",
        "https://us.api.blizzard.com",
        "dynamic-us",
    ),
    "eu": RegionConfig(
        "eu",
        "https://eu.battle.net/oauth/token",
        "https://eu.api.blizzard.com",
        "dynamic-eu",
    ),
    "kr": RegionConfig(
        "kr",
        "https://kr.battle.net/oauth/token",
        "https://kr.api.blizzard.com",
        "dynamic-kr",
    ),
    "tw": RegionConfig(
        "tw",
        "https://tw.battle.net/oauth/token",
        "https://tw.api.blizzard.com",
        "dynamic-tw",
    ),
}


def _coerce_positive_int(value: Any) -> int | None:
    """Read a positive integer, rejecting anything that is not one.

    A missing or zero id must never look like a real entity, so nothing here
    falls back to a default that could be mistaken for data.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


class BlizzardClientError(Exception):
    """Base class for transport level failures."""


class BlizzardApiError(BlizzardClientError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.headers = dict(headers or {})



@dataclass(frozen=True)
class BlizzardJsonResult:
    http_status: int
    payload: Mapping[str, Any]
    headers: Mapping[str, str]
    #: Exact response body, so the raw artifact can be stored verbatim.
    raw_text: str = ""
    #: True when the answer came from the disk cache instead of the network.
    from_cache: bool = False


#: Header names Blizzard may use for its two documented limits. Captured on
#: every response purely as evidence: what a real 429, or a degraded (hourly)
#: response, actually carries -- so the pacer can be tuned to fact, not to a
#: guess about which header name to trust.
_THROTTLE_HEADER_HINTS = (
    "retry-after",
    "throttle",
    "quota",
    "limit",
    "reset",
    "remain",
    "rate",
)


def _blizzard_rate_limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    captured: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower.startswith(("battlenet-", "x-plan-", "x-ratelimit")) or any(
            hint in lower for hint in _THROTTLE_HEADER_HINTS
        ):
            captured[key] = value
    return captured




class AsyncBlizzardClient:
    def __init__(
        self,
        region: str = "eu",
        locale: str = "en_GB",
        namespace: str | None = None,
        client_id: str = BLIZZARD_CLIENT_ID,
        client_secret: str = BLIZZARD_CLIENT_SECRET,
        timeout_seconds: float = 30.0,
        concurrency: int = 8,
        max_retries: int = 3,
        cache: Any = None,
        cache_ttl: float = 0.0,
        offline: bool = False,
    ) -> None:
        if aiohttp is None:
            raise BlizzardApiError(
                "Instale a dependência assíncrona: python -m pip install aiohttp"
            )
        try:
            self.region_config = REGION_CONFIGS[region.casefold()]
        except KeyError as exc:
            choices = ", ".join(sorted(REGION_CONFIGS))
            raise BlizzardApiError(f"Região inválida. Use: {choices}.") from exc
        if timeout_seconds <= 0:
            raise ValueError("timeout deve ser maior que zero.")
        if concurrency <= 0:
            raise ValueError("concurrency deve ser maior que zero.")
        if max_retries < 0:
            raise ValueError("max_retries não pode ser negativo.")

        self.locale = locale
        self.namespace = namespace or self.region_config.namespace
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self.concurrency = concurrency
        self.max_retries = max_retries
        #: a ``woweconomy.services.http_cache.ResponseCache`` or None
        self.cache = cache
        #: serve a cached answer without asking, while it is younger than this
        self.cache_ttl = cache_ttl
        #: answer only from the cache; a missing entry is an error
        self.offline = offline
        self._session: Any = None
        self._semaphore: asyncio.Semaphore | None = None
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    async def __aenter__(self) -> Self:
        connector = aiohttp.TCPConnector(
            limit=self.concurrency,
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._semaphore = asyncio.Semaphore(self.concurrency)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None
        self._semaphore = None

    async def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        result = await self.get_json_with_meta(path, params)
        return result.payload

    def _from_cache(self, entry) -> BlizzardJsonResult:
        # a cached answer is still the answer: the same bytes, the same parse
        return BlizzardJsonResult(
            http_status=int(entry.status),
            payload=entry.payload(),
            headers={},
            raw_text=entry.raw_text,
            from_cache=True,
        )

    async def get_json_with_meta(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> BlizzardJsonResult:
        request_url = (
            path
            if path.startswith("https://")
            else f"{self.region_config.api_url}{path}"
        )
        entry = None
        if self.cache is not None:
            entry = self.cache.get(request_url, params)
            if entry is not None and (
                    self.offline or self.cache.is_fresh(entry, self.cache_ttl)):
                return self._from_cache(entry)
            if self.offline:
                raise BlizzardApiError(
                    f"não há resposta em cache para {request_url} (modo offline)",
                    http_status=None)

        if self._session is None or self._semaphore is None:
            raise BlizzardApiError("A sessão assíncrona da Blizzard não está aberta.")

        token = await self._get_access_token()
        refreshed_token = False
        conditional: dict[str, str] = {}
        if entry is not None:
            # Ask whether it changed, not for the body again: a 304 has none.
            if entry.etag:
                conditional["If-None-Match"] = entry.etag
            elif entry.last_modified:
                conditional["If-Modified-Since"] = entry.last_modified
        for attempt in range(self.max_retries + 2):
            try:
                async with (
                    self._semaphore,
                    self._session.get(
                        request_url,
                        params=dict(params or {}),
                        headers={"Authorization": f"Bearer {token}", **conditional},
                    ) as response,
                ):
                    status = response.status
                    captured = _blizzard_rate_limit_headers(response.headers)
                    if status == 304 and entry is not None:
                        self.cache.touch(entry)
                        return self._from_cache(entry)
                    if status == 200:
                        # The exact bytes are kept so ingestion can store the
                        # source artifact itself, not a re-serialised guess of it.
                        raw_text = await response.text()
                        try:
                            payload = json.loads(raw_text)
                        except ValueError as exc:
                            raise BlizzardApiError(
                                "A Blizzard devolveu uma resposta JSON inválida.",
                                http_status=status,
                                headers=captured,
                            ) from exc
                        if not isinstance(payload, Mapping):
                            raise BlizzardApiError(
                                "A resposta da Blizzard não é um objeto JSON.",
                                http_status=status,
                                headers=captured,
                            )
                        if self.cache is not None:
                            self.cache.put(
                                request_url, params, status=status,
                                raw_text=raw_text,
                                etag=response.headers.get("ETag", ""),
                                last_modified=response.headers.get("Last-Modified", ""))
                        return BlizzardJsonResult(
                            http_status=status,
                            payload=payload,
                            headers=captured,
                            raw_text=raw_text,
                        )

                    if status == 401 and not refreshed_token:
                        async with self._token_lock:
                            if self._access_token == token:
                                self._access_token = None
                        token = await self._get_access_token()
                        refreshed_token = True
                        continue
                    if status not in {429, 500, 502, 503, 504}:
                        raise BlizzardApiError(
                            f"A Blizzard respondeu HTTP {status} ({response.reason}).",
                            http_status=status,
                            headers=captured,
                        )
                    if attempt >= self.max_retries:
                        raise BlizzardApiError(
                            f"A Blizzard respondeu HTTP {status} após "
                            f"{self.max_retries + 1} tentativas.",
                            http_status=status,
                            headers=captured,
                        )
                    retry_after = response.headers.get("Retry-After")
                await self._wait_before_retry(retry_after, attempt)
            except BlizzardApiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise BlizzardApiError(
                        f"Falha de rede ao contactar a Blizzard: {exc}"
                    ) from exc
                await self._wait_before_retry(None, attempt)

        raise BlizzardApiError("A Blizzard não devolveu uma resposta utilizável.")

    async def count_item_auctions(
        self,
        connected_realm_id: int,
        item_id: int,
    ) -> int:
        payload = await self.get_json(
            f"/data/wow/connected-realm/{connected_realm_id}/auctions",
            {"namespace": self.namespace, "locale": self.locale},
        )
        auctions = payload.get("auctions")
        if not isinstance(auctions, list):
            raise BlizzardApiError(
                "A resposta da Blizzard não contém uma lista 'auctions'."
            )
        return sum(
            _coerce_positive_int(auction.get("item", {}).get("id")) == item_id
            for auction in auctions
            if isinstance(auction, Mapping) and isinstance(auction.get("item"), Mapping)
        )

    async def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token
            if not self.client_id or not self.client_secret:
                raise BlizzardApiError(
                    "Defina BLIZZARD_CLIENT_ID e BLIZZARD_CLIENT_SECRET "
                    "antes de usar a API."
                )
            if self._session is None:
                raise BlizzardApiError(
                    "A sessão assíncrona da Blizzard não está aberta."
                )

            for attempt in range(self.max_retries + 1):
                try:
                    async with self._session.post(
                        self.region_config.token_url,
                        data={
                            "grant_type": "client_credentials",
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    ) as response:
                        status = response.status
                        if status == 200:
                            try:
                                payload = await response.json(content_type=None)
                            except (ValueError, aiohttp.ContentTypeError) as exc:
                                raise BlizzardApiError(
                                    "A resposta do OAuth da Blizzard é inválida."
                                ) from exc
                            token = (
                                payload.get("access_token")
                                if isinstance(payload, Mapping)
                                else None
                            )
                            if not isinstance(token, str) or not token:
                                raise BlizzardApiError(
                                    "A Blizzard não devolveu um access_token válido."
                                )
                            expires_in = _coerce_positive_int(payload.get("expires_in"))
                            if expires_in is None or expires_in <= 0:
                                expires_in = 300
                            self._access_token = token
                            self._token_expires_at = time.monotonic() + max(
                                0.5, expires_in * 0.8
                            )
                            return token
                        if status not in {429, 500, 502, 503, 504}:
                            raise BlizzardApiError(
                                f"O OAuth da Blizzard respondeu HTTP {status} "
                                f"({response.reason})."
                            )
                        if attempt >= self.max_retries:
                            raise BlizzardApiError(
                                "Não foi possível obter o token OAuth da Blizzard."
                            )
                        retry_after = response.headers.get("Retry-After")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt >= self.max_retries:
                        raise BlizzardApiError(
                            f"Falha de rede durante o OAuth da Blizzard: {exc}"
                        ) from exc
                    retry_after = None
                await self._wait_before_retry(retry_after, attempt)

        raise BlizzardApiError("Não foi possível obter o token OAuth da Blizzard.")

    async def _wait_before_retry(
        self,
        retry_after: str | None,
        attempt: int,
    ) -> None:
        delay = 0.5 * (2**attempt)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        await asyncio.sleep(min(delay, 30.0))

