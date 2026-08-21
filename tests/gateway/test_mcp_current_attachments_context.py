"""Current Telegram files stay inside their own turn."""

import asyncio

from gateway.session_context import (
    clear_session_vars,
    current_attachments_meta,
    set_current_attachments,
    set_session_vars,
)


def test_current_attachments_do_not_leak_between_concurrent_turns():
    async def turn(message_id, path, ready, release):
        tokens = set_session_vars(platform="telegram", message_id=message_id)
        set_current_attachments([path], ["application/pdf"])
        ready.set()
        await release.wait()
        try:
            return current_attachments_meta()
        finally:
            clear_session_vars(tokens)

    async def scenario():
        first_ready = asyncio.Event()
        second_ready = asyncio.Event()
        release = asyncio.Event()
        first = asyncio.create_task(turn("1", "/cache/one.pdf", first_ready, release))
        second = asyncio.create_task(turn("2", "/cache/two.pdf", second_ready, release))
        await first_ready.wait()
        await second_ready.wait()
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(scenario())

    assert first["ai.hermes/current-attachments"]["message_id"] == "1"
    assert first["ai.hermes/current-attachments"]["files"][0]["path"] == "/cache/one.pdf"
    assert second["ai.hermes/current-attachments"]["message_id"] == "2"
    assert second["ai.hermes/current-attachments"]["files"][0]["path"] == "/cache/two.pdf"


def test_clearing_a_turn_removes_its_attachments():
    tokens = set_session_vars(platform="telegram", message_id="1")
    set_current_attachments(["/cache/one.pdf"], ["application/pdf"])

    clear_session_vars(tokens)

    assert current_attachments_meta() is None


def test_binding_a_new_turn_clears_inherited_attachments():
    parent_tokens = set_session_vars(platform="telegram", message_id="old")
    set_current_attachments(["/cache/old.pdf"], ["application/pdf"])

    async def new_turn():
        tokens = set_session_vars(platform="telegram", message_id="new")
        try:
            return current_attachments_meta()
        finally:
            clear_session_vars(tokens)

    try:
        assert asyncio.run(new_turn()) is None
    finally:
        clear_session_vars(parent_tokens)
