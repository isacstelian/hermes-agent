"""Rate limit on the home-channel shutdown broadcast.

A one-shot "restart all gateways" job left under launchd's KeepAlive ran 55
times on 2026-07-30. Each pass SIGTERM'd 19 gateways, and each gateway told the
home channel it was shutting down: 144 identical pings in the owner's Telegram,
while the bots were in fact unusable.

None of the existing suppressions covered it. The SIGTERM was not an in-chat
``/restart``, not a planned restart, and carried no drain marker -- an external
restarter looks like nothing Hermes knows about. So the cap is unconditional and
sits last, after every other suppression has had its say.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as run_module
from gateway.config import HomeChannel, Platform
from tests.gateway.restart_test_helpers import make_restart_runner


class TestCooldownMarker:
    def test_absent_marker_is_not_a_cooldown(self, shutdown_broadcast_marker):
        assert run_module.shutdown_broadcast_in_cooldown() is False

    def test_recorded_broadcast_starts_a_cooldown(self, shutdown_broadcast_marker):
        run_module.record_shutdown_broadcast(now=1000.0)

        assert run_module.shutdown_broadcast_in_cooldown(now=1000.0) is True
        assert run_module.shutdown_broadcast_in_cooldown(now=1299.0) is True

    def test_cooldown_expires_at_the_window(self, shutdown_broadcast_marker):
        run_module.record_shutdown_broadcast(now=1000.0)

        assert run_module.shutdown_broadcast_in_cooldown(now=1300.0) is False
        assert run_module.shutdown_broadcast_in_cooldown(now=9000.0) is False

    def test_corrupt_marker_fails_towards_notifying(self, shutdown_broadcast_marker):
        shutdown_broadcast_marker.write_text("{not json")

        assert run_module.shutdown_broadcast_in_cooldown() is False

    def test_marker_without_the_field_fails_towards_notifying(self, shutdown_broadcast_marker):
        shutdown_broadcast_marker.write_text(json.dumps({"sent": "yes"}))

        assert run_module.shutdown_broadcast_in_cooldown() is False

    def test_marker_dated_in_the_future_does_not_silence_us(self, shutdown_broadcast_marker):
        """A clock step or a restored backup must not mute shutdowns for hours."""

        run_module.record_shutdown_broadcast(now=50_000.0)

        assert run_module.shutdown_broadcast_in_cooldown(now=1000.0) is False


class TestBroadcastLoop:
    @staticmethod
    def _runner_with_home_channel():
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=None)
        runner, _ = make_restart_runner()
        runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
            platform=Platform.TELEGRAM,
            chat_id="5691125996",
            name="acasă",
        )
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner.session_store = None
        runner._snapshot_running_agents = lambda: {}
        runner._thread_metadata_for_target = lambda *a, **k: None
        runner._notify_active_sessions_of_shutdown = (
            run_module.GatewayRunner._notify_active_sessions_of_shutdown.__get__(
                runner, run_module.GatewayRunner
            )
        )
        return runner, adapter

    @pytest.mark.asyncio
    async def test_first_shutdown_notifies_home_channel(self, shutdown_broadcast_marker):
        runner, adapter = self._runner_with_home_channel()

        await runner._notify_active_sessions_of_shutdown()

        assert adapter.send.call_count == 1
        assert run_module.shutdown_broadcast_in_cooldown() is True

    @pytest.mark.asyncio
    async def test_restart_storm_notifies_once(self, shutdown_broadcast_marker):
        """Ten rapid restarts, one message -- the whole point of the change."""

        runner, adapter = self._runner_with_home_channel()

        for _ in range(10):
            await runner._notify_active_sessions_of_shutdown()

        assert adapter.send.call_count == 1

    @pytest.mark.asyncio
    async def test_shutdown_after_the_window_notifies_again(self, shutdown_broadcast_marker):
        runner, adapter = self._runner_with_home_channel()
        run_module.record_shutdown_broadcast(
            now=0.0 - run_module.SHUTDOWN_BROADCAST_COOLDOWN_SECONDS
        )

        await runner._notify_active_sessions_of_shutdown()

        assert adapter.send.call_count == 1
