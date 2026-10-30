from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from src.config.models import ProviderConfig, UserConfig
from src.providers.catalog import CatalogUnavailable, OpenRouterModel, list_openrouter_models
from src.service.chat import ChatService
from src.web.app import create_app


def _model(model_id: str, name: str, output: str = "text") -> dict:
    return {
        "id": model_id, "name": name, "description": "A model",
        "architecture": {"output_modalities": [output]},
    }


def _endpoint(model_id: str, provider: str = "Private", status: int = 0) -> dict:
    return {
        "model_id": model_id, "provider_name": provider, "status": status,
        "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
    }


class ModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = [
            _model("vendor/beta", "Beta"),
            _model("vendor/alpha", "Alpha"),
            _model("vendor/no-zdr", "No ZDR"),
            _model("vendor/offline", "Offline"),
            _model("vendor/image", "Image", "image"),
        ]
        self.endpoints = [
            _endpoint("vendor/beta"),
            _endpoint("vendor/alpha", "Z provider"),
            _endpoint("vendor/alpha", "A provider"),
            _endpoint("vendor/alpha", "A provider"),
            _endpoint("vendor/offline", status=-2),
            _endpoint("vendor/image"),
            _endpoint("vendor/not-in-catalog"),
        ]
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.invalid: object | None = None

        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": self.models})
            return httpx.Response(
                self.status, json=self.invalid if self.invalid is not None else {"data": self.endpoints},
            )

        client_class = httpx.Client
        client_patch = patch(
            "src.providers.catalog.httpx.Client",
            side_effect=lambda **_kwargs: client_class(transport=httpx.MockTransport(respond)),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def test_only_text_models_with_active_zdr_endpoints_are_returned(self) -> None:
        models = [model.model_dump() for model in list_openrouter_models()]
        self.assertEqual(models, [
            {
                "id": "vendor/alpha", "name": "Alpha", "description": "A model",
                "zdr_providers": ["A provider", "Z provider"],
                "pricing": {"prompt": "0.1", "completion": "0.5"},
                "input_modalities": [],
                "parameter_routes": [[], [], []],
            },
            {
                "id": "vendor/beta", "name": "Beta", "description": "A model",
                "zdr_providers": ["Private"],
                "pricing": {"prompt": "0.1", "completion": "0.5"},
                "input_modalities": [],
                "parameter_routes": [[]],
            },
        ])
        self.assertEqual([request.url.path for request in self.requests], [
            "/api/v1/models", "/api/v1/endpoints/zdr",
        ])

    def test_search_is_case_insensitive_by_name_and_does_not_leave_process(self) -> None:
        self.assertEqual([model.id for model in list_openrouter_models("  ALpHa  ")], ["vendor/alpha"])
        for request in self.requests:
            self.assertFalse(request.url.query)
            self.assertNotIn("authorization", request.headers)

    def test_search_by_id(self) -> None:
        self.assertEqual([model.id for model in list_openrouter_models("VENDOR/BETA")], ["vendor/beta"])

    def test_empty_search_result_is_not_a_failure(self) -> None:
        self.assertEqual(list_openrouter_models("missing"), [])

    def test_empty_zdr_policy_never_returns_unrestricted_models(self) -> None:
        self.endpoints.clear()
        self.assertEqual(list_openrouter_models(), [])

    def test_upstream_http_error_never_returns_unrestricted_models(self) -> None:
        self.status = 503
        with self.assertRaisesRegex(CatalogUnavailable, "HTTP 503"):
            list_openrouter_models()

    def test_malformed_zdr_policy_fails_explicitly(self) -> None:
        self.invalid = {"data": [{"model_id": "vendor/alpha", "provider_name": "Unknown"}]}
        with self.assertRaisesRegex(CatalogUnavailable, "invalid catalog or ZDR policy"):
            list_openrouter_models()

    def test_missing_data_is_not_treated_as_an_empty_catalog(self) -> None:
        self.invalid = {}
        with self.assertRaises(CatalogUnavailable):
            list_openrouter_models()

    def test_invalid_models_catalog_fails_before_fetching_policy(self) -> None:
        self.models[0].pop("architecture")
        with self.assertRaises(CatalogUnavailable):
            list_openrouter_models()
        self.assertEqual(len(self.requests), 1)

    def test_connection_failure_is_actionable(self) -> None:
        with patch("src.providers.catalog.httpx.Client", side_effect=httpx.ConnectError("offline")):
            with self.assertRaisesRegex(CatalogUnavailable, "check connectivity"):
                list_openrouter_models()

    def test_prices_use_cheapest_active_zdr_route_for_each_token_direction(self) -> None:
        self.endpoints[1]["pricing"] = {"prompt": "0.0000009", "completion": "0.0000002"}
        self.endpoints[2]["pricing"] = {"prompt": "0.0000003", "completion": "0.0000008"}
        self.endpoints[3]["pricing"] = {"prompt": "0.0000006", "completion": "0.0000007"}
        inactive = _endpoint("vendor/alpha", status=-2)
        inactive["pricing"] = {"prompt": "0", "completion": "0"}
        self.endpoints.append(inactive)
        self.models[1]["pricing"] = {"prompt": "0", "completion": "0"}
        pricing = list_openrouter_models("alpha")[0].pricing
        self.assertEqual(pricing.model_dump(), {"prompt": "0.3", "completion": "0.2"})
        self.assertEqual(list_openrouter_models("alpha", zdr=False)[0].pricing.model_dump(), {
            "prompt": "0", "completion": "0",
        })

    def test_price_conversion_preserves_decimal_precision_and_free_prices(self) -> None:
        self.endpoints[0]["pricing"] = {"prompt": "0.000000123456789", "completion": "0"}
        pricing = list_openrouter_models("beta")[0].pricing
        self.assertEqual(pricing.model_dump(), {"prompt": "0.123456789", "completion": "0"})

    def test_missing_and_dynamic_prices_are_unavailable_not_free(self) -> None:
        self.endpoints[0]["pricing"] = {"prompt": "-1"}
        pricing = list_openrouter_models("beta")[0].pricing
        self.assertEqual(pricing.model_dump(), {"prompt": None, "completion": None})
        self.endpoints[0].pop("pricing")
        self.assertEqual(list_openrouter_models("beta")[0].pricing.model_dump(), {
            "prompt": None, "completion": None,
        })

    def test_invalid_or_nonfinite_prices_fail_explicitly(self) -> None:
        for price in ("not-a-price", "NaN", "Infinity"):
            with self.subTest(price=price):
                self.endpoints[0]["pricing"] = {"prompt": price}
                with self.assertRaises(CatalogUnavailable):
                    list_openrouter_models()

    def test_explicit_opt_out_includes_non_zdr_text_models_and_global_prices(self) -> None:
        self.models[2]["pricing"] = {"prompt": "0.00000004", "completion": "0.00000012"}
        models = list_openrouter_models(zdr=False)
        ids = {model.id for model in models}
        self.assertIn("vendor/no-zdr", ids)
        self.assertNotIn("vendor/image", ids)
        model = next(model for model in models if model.id == "vendor/no-zdr")
        self.assertEqual(model.zdr_providers, [])
        self.assertEqual(model.pricing.model_dump(), {"prompt": "0.04", "completion": "0.12"})

    def test_capabilities_preserve_separate_active_zdr_routes(self) -> None:
        self.models[1]["architecture"]["input_modalities"] = ["text", "image"]
        self.models[1]["supported_parameters"] = ["tools", "reasoning"]
        self.endpoints[1]["supported_parameters"] = ["tools"]
        self.endpoints[2]["supported_parameters"] = ["reasoning_effort"]
        self.endpoints[3]["status"] = -2
        self.endpoints[3]["supported_parameters"] = ["tools", "reasoning"]
        model = list_openrouter_models("alpha")[0]
        self.assertEqual(model.input_modalities, ["text", "image"])
        self.assertEqual(model.parameter_routes, [["tools"], ["reasoning_effort"]])
        unrestricted = list_openrouter_models("alpha", zdr=False)[0]
        self.assertEqual(unrestricted.parameter_routes, [["tools", "reasoning"]])

    def test_malformed_capability_metadata_fails_explicitly(self) -> None:
        self.endpoints[0]["supported_parameters"] = "tools"
        with self.assertRaises(CatalogUnavailable):
            list_openrouter_models()


class ModelCatalogWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = UserConfig(
            id="test-user", name="Test", provider=ProviderConfig(name="openrouter"), agents=[],
        )
        self.service = ChatService(agent_factory=Mock())
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.cookies.set("recall_user", self.user.id)
        user_patch = patch("src.service.chat.load_user", return_value=self.user)
        self.load_user = user_patch.start()
        self.addCleanup(user_patch.stop)
        catalog_patch = patch("src.service.chat.list_openrouter_models", return_value=[
            OpenRouterModel(id="vendor/alpha", name="Alpha", description="Test", zdr_providers=["Private"]),
        ])
        self.catalog = catalog_patch.start()
        self.addCleanup(catalog_patch.stop)

    def test_catalog_response_shape_and_query(self) -> None:
        response = self.client.get("/api/models", params={"q": "alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{
            "id": "vendor/alpha", "name": "Alpha", "description": "Test", "zdr_providers": ["Private"],
            "pricing": {"prompt": None, "completion": None},
            "input_modalities": [],
            "parameter_routes": [],
        }])
        self.catalog.assert_called_once_with("alpha", zdr=True)
        self.load_user.assert_called_once_with("test-user")

    def test_login_is_required_before_catalog_lookup(self) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/models").status_code, 401)
        self.catalog.assert_not_called()

    def test_unknown_user_cannot_lookup_catalog(self) -> None:
        self.load_user.side_effect = FileNotFoundError()
        self.assertEqual(self.client.get("/api/models").status_code, 401)
        self.catalog.assert_not_called()

    def test_non_openrouter_user_gets_explicit_error(self) -> None:
        self.user.provider.name = "ollama"
        response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 400)
        self.assertIn("only available for the OpenRouter", response.json()["detail"])
        self.catalog.assert_not_called()

    def test_upstream_failure_is_502_not_a_success_shaped_empty_list(self) -> None:
        self.catalog.side_effect = CatalogUnavailable("OpenRouter catalog unavailable; retry later.")
        with self.assertLogs("recall.web", level="WARNING"):
            response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 502)
        self.assertIn("retry later", response.json()["detail"])

    def test_explicit_zdr_opt_out_is_forwarded_to_catalog(self) -> None:
        self.assertEqual(self.client.get("/api/models?zdr=false").status_code, 200)
        self.catalog.assert_called_once_with("", zdr=False)

    def test_invalid_zdr_query_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/models?zdr=maybe").status_code, 422)
        self.catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
