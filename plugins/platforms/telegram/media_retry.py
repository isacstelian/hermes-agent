"""Bounded Telegram media retries, kept outside polling-task teardown."""

import asyncio
import logging
import re
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from gateway.platforms.base import _thread_metadata_for_source

# Placeholder copy awaiting owner approval. Keep complete human notices together.
MEDIA_RETRY_NOTICES = {
    "voice": {
        "T1": "Telegram are o problemă chiar acum și nu-ți pot descărca mesajul vocal. Îl ascult singur imediat ce revine, nu trebuie să-l retrimiți.",
        "T2": "Nu reușesc nici acum să-ți descarc mesajul vocal de la {ora}. Mi-l poți retrimite?",
    },
    "audio": {
        "T1": "Telegram are o problemă chiar acum și nu-ți pot descărca fișierul audio. Îl ascult singur imediat ce revine, nu trebuie să-l retrimiți.",
        "T2": "Nu reușesc nici acum să-ți descarc fișierul audio de la {ora}. Mi-l poți retrimite?",
    },
    "document": {
        "T1": "Telegram are o problemă chiar acum și nu-ți pot descărca fișierul. Îl deschid singur imediat ce revine, nu trebuie să-l retrimiți.",
        "T2": "Nu reușesc nici acum să-ți descarc fișierul de la {ora}. Mi-l poți retrimite?",
    },
    "photo": {
        "T1": "Telegram are o problemă chiar acum și nu-ți pot descărca poza. O deschid singur imediat ce revine, nu trebuie s-o retrimiți.",
        "T2": "Nu reușesc nici acum să-ți descarc poza de la {ora}. Mi-o poți retrimite?",
    },
    "video": {
        "T1": "Telegram are o problemă chiar acum și nu-ți pot descărca clipul. Îl deschid singur imediat ce revine, nu trebuie să-l retrimiți.",
        "T2": "Nu reușesc nici acum să-ți descarc clipul de la {ora}. Mi-l poți retrimite?",
    },
}

MEDIA_RETRY_INITIAL_DELAY = 2.0
MEDIA_RETRY_MAX_DELAY = 180.0
MEDIA_RETRY_WINDOW = 30 * 60.0
MEDIA_RETRY_T1_AFTER = 120.0
MEDIA_RETRY_LATE_AFTER = 60.0
MEDIA_RETRY_T2_DELAY = 200.0
MEDIA_RETRY_T2_RETRIES = 3

# A retryable-fatal disconnect removes the adapter from GatewayRunner; its
# watcher creates a NEW adapter. Reattach pending work only when that adapter
# becomes connected, scoped by event loop, profile home and bot credential.
# Weak values avoid retaining idle adapters or completed test loops.
_owners = weakref.WeakValueDictionary()


def retry_after(error):
    value = getattr(error, "retry_after", 0)
    if hasattr(value, "total_seconds"):
        value = value.total_seconds()
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def is_transient(adapter, error):
    if adapter._looks_like_network_error(error):
        return True
    if error.__class__.__name__ == "RetryAfter":
        return True
    # BadRequest inherits NetworkError in PTB; exclude semantic errors before
    # inspecting transport messages/statuses, even if they mention a 5xx.
    if adapter._is_bad_request_error(error) or error.__class__.__name__ in {
        "Forbidden", "InvalidToken",
    }:
        return False
    import httpx

    if isinstance(error, httpx.TransportError):
        return True
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    return bool(re.search(r"bad gateway|\b5\d\d\b", str(error), re.IGNORECASE))


def sent_time(message):
    sent = message.date
    if not isinstance(sent, datetime):
        sent = datetime.now(timezone.utc)
    return sent.replace(tzinfo=timezone.utc) if sent.tzinfo is None else sent


def local_time(message):
    from hermes_time import get_timezone

    return sent_time(message).astimezone(get_timezone() or ZoneInfo("Europe/Bucharest")).strftime("%H:%M")


@dataclass
class IncomingRetry:
    message: object
    event: object
    kind: str
    arrived: float
    error: Exception
    attempts: int = 1
    t1_sent: bool = False
    lost: bool = False
    completed: bool = False
    task: object = None


class MediaRetries:
    def __init__(self, adapter):
        self.adapter = adapter
        self.incoming = {}

    @property
    def logger(self):
        # LOST must use the adapter's module logger (profile errors.log).
        return logging.getLogger(self.adapter.__class__.__module__)

    def schedule(self, message, event, kind, arrived, error):
        key = (event.source.chat_id, event.message_id)
        if key in self.incoming:
            return
        state = IncomingRetry(message, event, kind, arrived, error)
        self.incoming[key] = state
        _owners[self.adapter._media_retry_key()] = self
        from .adapter import _redact_telegram_error_text

        self.logger.warning(
            "[Telegram] media-retry scheduled kind=%s chat=%s message=%s error=%s",
            kind, *key, _redact_telegram_error_text(error),
        )
        state.task = asyncio.create_task(self.run_incoming(key, state))

    def log_lost(self, state, reason):
        if state.lost or state.completed:
            return
        state.lost = True
        self.logger.error(
            "[Telegram] media-retry LOST kind=%s chat=%s message=%s sent_at=%s reason=%s",
            state.kind, state.event.source.chat_id, state.event.message_id,
            sent_time(state.message).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reason,
        )

    async def notice(self, state, stage):
        try:
            adapter = self.adapter
            result = await adapter.send(
                state.event.source.chat_id,
                MEDIA_RETRY_NOTICES[state.kind][stage].format(ora=local_time(state.message)),
                reply_to=state.event.message_id,
                metadata=_thread_metadata_for_source(state.event.source, state.event.message_id),
            )
            return result.success
        except Exception as error:
            from .adapter import _redact_telegram_error_text

            self.logger.warning("[Telegram] media-retry notice failed: %s", _redact_telegram_error_text(error))
            return False

    async def run_incoming(self, key, state):
        delay = MEDIA_RETRY_INITIAL_DELAY
        deadline = state.arrived + MEDIA_RETRY_WINDOW
        try:
            if (self.adapter._media_retry_now() < deadline and
                    self.adapter._media_retry_now() - state.arrived >= MEDIA_RETRY_T1_AFTER):
                state.t1_sent = await self.notice(state, "T1")
            while True:
                adapter = self.adapter
                remaining = deadline - adapter._media_retry_now()
                if remaining <= 0:
                    break
                pause = max(delay, retry_after(state.error))
                await adapter._media_retry_sleep(min(pause, remaining))
                adapter = self.adapter
                if adapter._media_retry_now() >= deadline:
                    break
                state.attempts += 1
                try:
                    await adapter._cache_and_dispatch_media(state.message, state.event, retry=state)
                except Exception as error:
                    if not is_transient(adapter, error):
                        raise
                    state.error = error
                    if (not state.t1_sent and
                            adapter._media_retry_now() - state.arrived >= MEDIA_RETRY_T1_AFTER):
                        state.t1_sent = await self.notice(state, "T1")
                    delay = min(delay * 2, MEDIA_RETRY_MAX_DELAY)
                    continue
                state.completed = True
                if state.event.media_urls:
                    self.logger.info(
                        "[Telegram] media-retry delivered kind=%s chat=%s message=%s attempts=%d after=%.0fs",
                        state.kind, *key, state.attempts, adapter._media_retry_now() - state.arrived,
                    )
                return

            self.log_lost(state, "window")
            # Caption dispatch must not wait another ten minutes for T2 retries.
            sent = await self.notice(state, "T2")
            if state.message.caption or state.message.text:
                state.event.text = self.adapter._append_observed_note(
                    state.event.text,
                    f"[The user's {state.kind} could not be downloaded after 30 minutes; "
                    "the gateway already asked them to resend it. Do not ask again.]",
                )
                await self.adapter.handle_message(state.event)
            for _ in range(MEDIA_RETRY_T2_RETRIES):
                if sent:
                    break
                await self.adapter._media_retry_sleep(MEDIA_RETRY_T2_DELAY)
                sent = await self.notice(state, "T2")
        except asyncio.CancelledError:
            self.log_lost(state, "shutdown")
            raise
        except Exception as error:
            from .adapter import _redact_telegram_error_text

            self.logger.error("[Telegram] media-retry failed: %s", _redact_telegram_error_text(error))
            self.log_lost(state, "window")
        finally:
            self.incoming.pop(key, None)

    async def shutdown(self):
        tasks = []
        for state in list(self.incoming.values()):
            self.log_lost(state, "shutdown")
            state.task.cancel()
            tasks.append(state.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class MediaRetryMixin:
    _media_retry_now = staticmethod(time.monotonic)
    _media_retry_sleep = staticmethod(asyncio.sleep)

    def _media_retry_key(self):
        return (asyncio.get_running_loop(), self._hermes_home, self.config.token)

    def _media_retry_manager(self):
        manager = getattr(self, "_media_retries", None)
        if manager is None:
            manager = MediaRetries(self)
            self._media_retries = manager
        return manager

    def _adopt_media_retries(self):
        # Some unit callers mark connected without an event loop.
        try:
            manager = _owners.get(self._media_retry_key())
        except RuntimeError:
            return
        if manager is not None:
            manager.adapter = self
            self._media_retries = manager

    async def _get_current_media_file(self, media):
        if self._bot is None:
            raise ConnectionError("Telegram reconnecting")
        return await self._bot.get_file(media.file_id)

    def _defer_media_failure(self, msg, event, kind, error, arrived, retry):
        if not is_transient(self, error):
            return False
        if retry is not None:
            raise error
        self._media_retry_manager().schedule(msg, event, kind, arrived, error)
        return True

    def _note_media_retry_delivery(self, event, retry):
        if retry is None or not event.media_urls:
            return
        if self._media_retry_now() - retry.arrived > MEDIA_RETRY_LATE_AFTER:
            note = (
                f"[This {retry.kind} message was sent at {local_time(retry.message)} "
                "and arrived late because Telegram was unavailable."
            )
            if retry.t1_sent:
                note += " The user was told it would be handled automatically and does not need to resend it."
            event.text = self._append_observed_note(event.text, note + "]")
