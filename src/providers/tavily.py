from __future__ import annotations

import ipaddress
import os
import re
import socket
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator

from src.config.models import TavilyConfig
from src.config.secrets import get_secret
from src.utils.logger import get_logger

logger = get_logger("provider-tavily")


class TavilyError(RuntimeError):
    """A failed Tavily request or an invalid provider response."""


class WebSearchResult(BaseModel):
    """A search hit with source metadata and an explicitly bounded excerpt."""

    title: str
    url: HttpUrl
    content: str
    published_date: str | None = None
    truncated: bool = False


class WebPage(BaseModel):
    """Extracted page text, optionally focused on a query."""

    url: HttpUrl
    raw_content: str
    truncated: bool = False


class _SearchResponse(BaseModel):
    results: list[WebSearchResult]


class _ExtractResponse(BaseModel):
    results: list[WebPage]
    failed_results: list[dict] = Field(default_factory=list)


TimeRange = Literal["day", "week", "month", "year"]
Topic = Literal["general", "news", "finance"]
_DOMAIN_PATTERN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


class _SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10, strict=True)
    topic: Topic = "general"
    time_range: TimeRange | None = None
    start_date: date | None = None
    end_date: date | None = None
    country: str | None = Field(default=None, min_length=2, max_length=64, pattern=r"^[a-z ]+$")
    include_domains: list[str] = Field(default_factory=list, max_length=300)
    exclude_domains: list[str] = Field(default_factory=list, max_length=150)

    @field_validator("country", mode="before")
    @classmethod
    def _normalize_country(cls, value):
        if isinstance(value, str):
            value = value.strip().lower() or None
        return value

    @field_validator("include_domains", "exclude_domains", mode="before")
    @classmethod
    def _normalize_domains(cls, value):
        if value is None:
            return []
        domains = []
        for item in value:
            domain = str(item).strip().lower()
            if not _DOMAIN_PATTERN.fullmatch(domain):
                raise ValueError(f"domain filter must be a bare domain such as example.com, got {item!r}")
            domains.append(domain)
        return list(dict.fromkeys(domains))

    @model_validator(mode="after")
    def _check_filters(self):
        if self.time_range and (self.start_date or self.end_date):
            raise ValueError("use either time_range or start_date/end_date, not both")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.country and self.topic != "general":
            raise ValueError("country filter is only available for the general topic")
        overlap = set(self.include_domains) & set(self.exclude_domains)
        if overlap:
            raise ValueError(f"domains cannot be both included and excluded: {sorted(overlap)}")
        return self


def _validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"} or not host
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in {None, 80, 443}
        or host.rstrip(".").lower() == "localhost"
        or host.rstrip(".").lower().endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("web retrieval requires a public HTTP(S) URL without credentials on port 80 or 443")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("web retrieval hostname could not be resolved") from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("web retrieval cannot access private, loopback or reserved addresses")
    return url


class TavilyClient:
    """Search and extract public web content without accessing the agent's data plane."""

    def __init__(self, api_key: str, config: TavilyConfig):
        if not api_key.strip():
            raise ValueError("Tavily API key must not be empty")
        self._api_key = api_key.strip()
        self._timeout = config.timeout_seconds
        self._max_content_chars = config.max_content_chars

    @classmethod
    def from_config(cls, config: TavilyConfig) -> TavilyClient | None:
        """Resolve the shared key at runtime; an unset key leaves web access unavailable."""
        api_key = get_secret(config.api_key)
        if not api_key.strip():
            logger.info("web access unavailable: env var %s is not set", config.api_key)
            return None
        return cls(api_key, config)

    def _post(self, endpoint: str, payload: dict) -> dict:
        try:
            with requests.post(
                f"https://api.tavily.com/{endpoint}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=(5, self._timeout + 5),
                allow_redirects=False,
            ) as response:
                status = response.status_code
                if status != 200:
                    advice = {
                        400: "check the search parameters",
                        401: "check the configured API key",
                        403: "check the API key permissions",
                        429: "rate limit reached; retry later",
                        432: "check the API key or plan usage limit",
                        433: "check the account's pay-as-you-go limit",
                    }.get(status, "check Tavily service availability and account limits")
                    raise TavilyError(f"Tavily {endpoint} failed (HTTP {status}); {advice}")
                data = response.json()
        except requests.Timeout:
            raise TavilyError(f"Tavily {endpoint} timed out; retry later") from None
        except requests.JSONDecodeError:
            raise TavilyError(f"Tavily {endpoint} returned invalid JSON") from None
        except requests.RequestException:
            raise TavilyError(f"Tavily {endpoint} connection failed; check network access") from None
        if not isinstance(data, dict):
            raise TavilyError(f"Tavily {endpoint} returned an invalid response object")
        return data

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        topic: str = "general",
        time_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        country: str | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[WebSearchResult]:
        """Find up to ten URLs with short excerpts, without generating an answer.

        Results without a detectable publication date are kept by Tavily even when a date
        filter is set; their published_date is None.
        """
        inputs = _SearchInput(
            query=query, top_k=top_k, topic=topic, time_range=time_range or None,
            start_date=start_date or None, end_date=end_date or None, country=country,
            include_domains=include_domains or [], exclude_domains=exclude_domains or [],
        )
        payload: dict = {
            "query": inputs.query,
            "max_results": inputs.top_k,
            "search_depth": "basic",
            "topic": inputs.topic,
            "auto_parameters": False,
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
        }
        if inputs.time_range:
            payload["time_range"] = inputs.time_range
        if inputs.start_date:
            payload["start_date"] = inputs.start_date.isoformat()
        if inputs.end_date:
            payload["end_date"] = inputs.end_date.isoformat()
        if inputs.country:
            payload["country"] = inputs.country
        if inputs.include_domains:
            payload["include_domains"] = inputs.include_domains
        if inputs.exclude_domains:
            payload["exclude_domains"] = inputs.exclude_domains
        data = self._post("search", payload)
        try:
            results = _SearchResponse.model_validate(data).results[:inputs.top_k]
        except ValidationError:
            raise TavilyError("Tavily search returned invalid search results") from None
        for result in results:
            result.truncated = len(result.content) > 2000
            result.content = result.content[:2000]
        return results

    def retrieve(self, url: str, query: str = "") -> WebPage:
        """Extract Markdown from a public URL, optionally selecting relevant passages."""
        url = _validate_public_url(str(HttpUrl(url)))
        if len(query) > 2000:
            raise ValueError("web retrieval query must not exceed 2000 characters")
        payload: dict = {
            "urls": [url],
            "extract_depth": "basic",
            "format": "markdown",
            "timeout": self._timeout,
        }
        if query.strip():
            payload.update(query=query.strip(), chunks_per_source=3)
        data = self._post("extract", payload)
        try:
            response = _ExtractResponse.model_validate(data)
        except ValidationError:
            raise TavilyError("Tavily extract returned invalid page results") from None
        if response.failed_results or len(response.results) != 1:
            raise TavilyError("Tavily could not extract the page; it may be blocked, unavailable or require login")
        page = response.results[0]
        if not page.raw_content.strip():
            raise TavilyError("Tavily extracted no readable content from the page")
        page.truncated = len(page.raw_content) > self._max_content_chars
        page.raw_content = page.raw_content[:self._max_content_chars]
        return page
