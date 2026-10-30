from __future__ import annotations

from decimal import Decimal
from typing import Iterable

import httpx
from pydantic import BaseModel, Field


class CatalogUnavailable(RuntimeError):
    """OpenRouter's model catalog or ZDR policy could not be retrieved or validated."""


class ModelPricing(BaseModel):
    """Minimum known input/output prices in USD per million tokens."""

    prompt: str | None = None
    completion: str | None = None


class OpenRouterModel(BaseModel):
    """A public chat model with its active ZDR routes and indicative prices."""

    id: str
    name: str
    description: str
    zdr_providers: list[str]
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    input_modalities: list[str] = Field(default_factory=list)
    parameter_routes: list[list[str]] = Field(default_factory=list)


class _Architecture(BaseModel):
    output_modalities: list[str]
    input_modalities: list[str] = Field(default_factory=list)


class _TokenPricing(BaseModel):
    prompt: Decimal | None = Field(default=None, allow_inf_nan=False)
    completion: Decimal | None = Field(default=None, allow_inf_nan=False)


class _Model(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    architecture: _Architecture
    pricing: _TokenPricing | None = None
    supported_parameters: list[str] = Field(default_factory=list)


class _ModelsResponse(BaseModel):
    data: list[_Model]


class _Endpoint(BaseModel):
    model_id: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    status: int = Field(strict=True)
    pricing: _TokenPricing | None = None
    supported_parameters: list[str] = Field(default_factory=list)


class _EndpointsResponse(BaseModel):
    data: list[_Endpoint]


def _price_per_million(prices: Iterable[Decimal | None]) -> str | None:
    known = [price for price in prices if price is not None and price >= 0]
    return format((min(known) * 1_000_000).normalize(), "f") if known else None


def list_openrouter_models(query: str = "", *, zdr: bool = True) -> list[OpenRouterModel]:
    """Search public chat models, requiring active ZDR routes unless explicitly disabled."""
    try:
        with httpx.Client(timeout=30) as client:
            models_response = client.get("https://openrouter.ai/api/v1/models")
            models_response.raise_for_status()
            models = _ModelsResponse.model_validate(models_response.json()).data
            endpoints_response = client.get("https://openrouter.ai/api/v1/endpoints/zdr")
            endpoints_response.raise_for_status()
            endpoints = _EndpointsResponse.model_validate(endpoints_response.json()).data
    except httpx.HTTPStatusError as exc:
        raise CatalogUnavailable(
            f"OpenRouter catalog request failed (HTTP {exc.response.status_code}); retry later."
        ) from exc
    except httpx.RequestError as exc:
        raise CatalogUnavailable("Could not reach the OpenRouter catalog; check connectivity and retry.") from exc
    except ValueError as exc:
        raise CatalogUnavailable("OpenRouter returned an invalid catalog or ZDR policy; retry later.") from exc

    routes: dict[str, list[_Endpoint]] = {}
    for endpoint in endpoints:
        if endpoint.status == 0:
            routes.setdefault(endpoint.model_id, []).append(endpoint)

    needle = query.strip().casefold()
    result = []
    for model in models:
        if (
            (zdr and model.id not in routes)
            or "text" not in model.architecture.output_modalities
            or (needle and needle not in model.id.casefold() and needle not in model.name.casefold())
        ):
            continue
        model_routes = routes.get(model.id, [])
        prices = [endpoint.pricing for endpoint in model_routes] if zdr else [model.pricing]
        result.append(OpenRouterModel(
            id=model.id,
            name=model.name,
            description=model.description,
            zdr_providers=sorted({endpoint.provider_name for endpoint in model_routes}),
            input_modalities=model.architecture.input_modalities,
            parameter_routes=(
                [endpoint.supported_parameters for endpoint in model_routes]
                if zdr else [model.supported_parameters]
            ),
            pricing=ModelPricing(
                prompt=_price_per_million(
                    price.prompt if price is not None else None for price in prices
                ),
                completion=_price_per_million(
                    price.completion if price is not None else None for price in prices
                ),
            ),
        ))
    return sorted(result, key=lambda model: (model.name.casefold(), model.id))
