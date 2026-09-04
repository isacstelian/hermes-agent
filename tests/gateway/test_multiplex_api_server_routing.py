"""Multiplex /p/<profile>/ routing for the api_server adapter.

Mirrors ``test_multiplex_http_routing.py`` (webhook): the default listener
owns the port, and secondary profiles are reached via a URL prefix when
``gateway.multiplex_profiles`` is on.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms import api_server as api_server_module
from gateway.platforms.api_server import (
    APIServerAdapter,
    _PROFILE_REJECTED,
    _api_request_profile,
)


def _make_adapter(
    multiplex: bool = True, allowlist: list[str] | None = None
) -> APIServerAdapter:
    cfg = PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 8642, "key": "test-key"})
    adapter = APIServerAdapter(cfg)

    class _Runner:
        config = GatewayConfig(
            multiplex_profiles=multiplex,
            multiplex_profile_allowlist=allowlist,
        )

    adapter.gateway_runner = _Runner()
    return adapter


class _FakeReq:
    def __init__(self, profile=None):
        self.match_info = {"profile": profile} if profile is not None else {}


class TestApiServerProfileResolution:
    def test_no_prefix_returns_none(self):
        adapter = _make_adapter(multiplex=True)
        assert adapter._resolve_request_profile(_FakeReq(None)) is None

    def test_unserved_prefix_is_rejected(self, monkeypatch):
        adapter = _make_adapter(multiplex=True, allowlist=["worker"])
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex, profile_allowlist=None: [
                ("default", "/profiles/default"),
                ("worker", "/profiles/worker"),
            ],
        )

        assert (
            adapter._resolve_request_profile(cast(Any, _FakeReq("worker")))
            == "worker"
        )
        assert (
            adapter._resolve_request_profile(cast(Any, _FakeReq("restricted")))
            is _PROFILE_REJECTED
        )


class TestApiServerRouteTable:
    def test_route_table_includes_models_options_and_chat(self):
        """Model discovery and chat routes must survive profile multiplexing."""
        adapter = _make_adapter(multiplex=True)
        paths = {path for _method, path, _handler in adapter._http_route_table()}
        assert "/v1/models" in paths
        assert "/api/model/options" in paths
        assert "/v1/chat/completions" in paths
        assert "/api/sessions/{session_id}/model" in paths
        # connect() mirrors every native path under /p/{profile}/…
        mirrored = {f"/p/{{profile}}{path}" for path in paths}
        assert "/p/{profile}/v1/models" in mirrored
        assert "/p/{profile}/api/model/options" in mirrored
        assert "/p/{profile}/v1/chat/completions" in mirrored
        assert "/p/{profile}/api/sessions/{session_id}/model" in mirrored


@pytest.mark.asyncio
async def test_detailed_health_reports_profile_and_manifest_hash(monkeypatch, tmp_path):
    profile_home = tmp_path / "magic-employee-support"
    profile_home.mkdir()
    manifest = {
        "profile": "magic-employee-support",
        "tools": ["magic_get_my_income_summary"],
        "schema_version": 1,
    }
    (profile_home / "capabilities.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    adapter = _make_adapter(multiplex=True)
    monkeypatch.setattr(adapter, "_check_auth", lambda request: None)
    monkeypatch.setattr(
        api_server_module,
        "collect_runtime_readiness",
        lambda **_kwargs: {"status": "ready"},
    )
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda: {})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "")

    token = _api_request_profile.set("magic-employee-support")
    try:
        response = await adapter._handle_health_detailed(SimpleNamespace())
    finally:
        _api_request_profile.reset(token)

    payload = json.loads(response.body)
    expected = hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert payload["profile"] == "magic-employee-support"
    assert payload["capability_manifest_sha256"] == expected


class TestApiServerModelsUnderProfile:
    def test_resolve_model_name_follows_active_profile(self, monkeypatch):
        """When the request is scoped to a named profile, advertise that name."""
        adapter = _make_adapter(multiplex=True)
        adapter._model_name = "hermes-agent"
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name",
            lambda: "coder",
        )
        token_prof = _api_request_profile.set("coder")
        try:
            assert adapter._resolve_model_name("") == "coder"
        finally:
            _api_request_profile.reset(token_prof)
