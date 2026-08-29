"""Fail-closed Telegram bot-origin interaction guard.

The guard is intentionally platform-specific: it consumes Telegram reply
lineage and keeps a short-lived in-process ledger of accepted inbound and
outbound message depths.  It never logs message, user, bot, or chat values.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping, Protocol, Sequence


logger = logging.getLogger(__name__)

DEDUP_TTL_SECONDS = 600.0
WINDOW_SECONDS = 60.0
PAIR_LIMIT = 1
CHAT_LIMIT = 6
BREAKER_VIOLATIONS = 2
BREAKER_SECONDS = 600.0
MESSAGE_DEPTH_TTL_SECONDS = 600.0
_VALID_POLICIES = frozenset({"none", "mentions", "all"})
_COMMAND_TARGET_RE = re.compile(r"(?:^|\s)/[A-Za-z0-9_]+@([A-Za-z0-9_]{3,32})\b")
_BOT_HANDLE_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_]{2,29}bot)\b", re.IGNORECASE)


class TelegramBotGuardConfigError(ValueError):
    """Raised when the Telegram bot-origin policy is unsafe or unknown."""


@dataclass(frozen=True)
class TelegramBotGuardDecision:
    allowed: bool
    reason: str
    depth: int | None = None


@dataclass
class TelegramBotGuardState:
    seen: MutableMapping[tuple[str, str, str, str], float] = field(default_factory=dict)
    message_depths: MutableMapping[tuple[str, str], tuple[int, float]] = field(
        default_factory=dict
    )
    pair_windows: MutableMapping[tuple[str, str, str, str], list[float]] = field(
        default_factory=dict
    )
    chat_windows: MutableMapping[tuple[str, str], list[float]] = field(
        default_factory=dict
    )
    violations: MutableMapping[tuple[str, str], list[float]] = field(
        default_factory=dict
    )
    breakers: MutableMapping[tuple[str, str], float] = field(default_factory=dict)


class TelegramBotGuardStore(Protocol):
    def load(self) -> TelegramBotGuardState:
        """Return the mutable state used for one atomic guard decision."""
        ...


class InMemoryTelegramBotGuardStore:
    """Process-local guard state.

    Guard decisions contain no ``await`` and therefore mutate this state
    atomically on the gateway event loop.
    """

    def __init__(self) -> None:
        self._state = TelegramBotGuardState()

    def load(self) -> TelegramBotGuardState:
        return self._state


class TelegramBotGuard:
    """Enforce Telegram bot-origin mention, lineage, rate, and breaker policy."""

    def __init__(
        self,
        *,
        policy: str,
        store: TelegramBotGuardStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized = str(policy or "").strip().lower()
        if normalized not in _VALID_POLICIES:
            raise TelegramBotGuardConfigError("invalid Telegram bot-origin policy")
        self.policy = normalized
        self._store = store or InMemoryTelegramBotGuardStore()
        self._clock = clock
        self._counters: Counter[str] = Counter()

    @property
    def counters(self) -> Mapping[str, int]:
        return dict(self._counters)

    def evaluate(
        self,
        event: Any,
        *,
        receiver_bot_id: str,
        receiver_username: str,
    ) -> TelegramBotGuardDecision:
        source = getattr(event, "source", None)
        if not bool(getattr(source, "is_bot", False)):
            self._note_human_inbound(event)
            return TelegramBotGuardDecision(True, "human", depth=0)

        if self.policy == "none":
            return self._decision(False, "policy_none")

        try:
            now = float(self._clock())
            state = self._load_state(now)
            profile = self._profile(event)
            chat_id = self._required(getattr(source, "chat_id", None))
            sender_bot_id = self._required(getattr(source, "user_id", None))
            receiver_id = self._required(receiver_bot_id)
            message_id = self._required(
                getattr(event, "message_id", None)
                or getattr(source, "message_id", None)
            )
            receiver_handle = self._required(receiver_username).lstrip("@").lower()
            chat_key = (profile, chat_id)

            breaker_until = state.breakers.get(chat_key)
            if breaker_until is not None and breaker_until > now:
                return self._decision(False, "breaker_open")

            dedup_key = (profile, chat_id, sender_bot_id, message_id)
            if state.seen.get(dedup_key, 0.0) > now:
                return self._decision(False, "duplicate_drop")

            raw_message = getattr(event, "raw_message", None)
            if raw_message is None:
                return self._depth_violation(state, chat_key, now, "unknown_lineage")

            command_mention = self._is_command_mention(event, receiver_handle)
            direct_reply = self._is_direct_reply(raw_message, receiver_id)
            if self.policy == "mentions" and not (command_mention or direct_reply):
                return self._decision(False, "mention_drop")

            depth = self._inbound_depth(
                state,
                event,
                chat_id=chat_id,
                receiver_bot_id=receiver_id,
                allow_unreplied_root=(command_mention or self.policy == "all"),
            )
            if depth is None:
                return self._depth_violation(state, chat_key, now, "unknown_lineage")
            if depth > 1:
                return self._depth_violation(
                    state,
                    chat_key,
                    now,
                    "depth_drop",
                    depth=depth,
                )

            pair_key = (profile, chat_id, sender_bot_id, receiver_id)
            pair_times = state.pair_windows.setdefault(pair_key, [])
            if len(pair_times) >= PAIR_LIMIT:
                return self._limit_violation(state, chat_key, now, "pair_rate_drop")

            chat_times = state.chat_windows.setdefault(chat_key, [])
            if len(chat_times) >= CHAT_LIMIT:
                return self._limit_violation(state, chat_key, now, "chat_rate_drop")

            state.seen[dedup_key] = now + DEDUP_TTL_SECONDS
            state.message_depths[(chat_id, message_id)] = (
                depth,
                now + MESSAGE_DEPTH_TTL_SECONDS,
            )
            pair_times.append(now)
            chat_times.append(now)
            return self._decision(True, "accept", depth=depth)
        except Exception:
            return self._decision(False, "state_error")

    def note_outbound(
        self,
        *,
        chat_id: str,
        message_ids: Sequence[str],
        reply_to_message_id: str | None,
        content: str,
    ) -> None:
        """Record Telegram messages emitted by this bot for reply lineage."""
        try:
            now = float(self._clock())
            state = self._load_state(now)
            chat_key = self._required(chat_id)
            reply_key = str(reply_to_message_id or "").strip()
            parent_depth: int | None = None
            if reply_key:
                parent = state.message_depths.get((chat_key, reply_key))
                if parent is not None:
                    parent_depth = parent[0]

            if parent_depth is not None and parent_depth >= 1:
                depth = parent_depth + 1
            else:
                addressed_to_peer = self.policy == "all" or self._addresses_any_bot(
                    content
                )
                depth = 1 if addressed_to_peer else 0

            for raw_message_id in message_ids:
                message_id = str(raw_message_id or "").strip()
                if not message_id:
                    continue
                state.message_depths[(chat_key, message_id)] = (
                    depth,
                    now + MESSAGE_DEPTH_TTL_SECONDS,
                )
        except Exception:
            return

    def _note_human_inbound(self, event: Any) -> None:
        """Best-effort root lineage capture; never changes human routing."""
        try:
            source = getattr(event, "source", None)
            now = float(self._clock())
            state = self._load_state(now)
            chat_id = self._required(getattr(source, "chat_id", None))
            message_id = self._required(
                getattr(event, "message_id", None)
                or getattr(source, "message_id", None)
            )
            state.message_depths[(chat_id, message_id)] = (
                0,
                now + MESSAGE_DEPTH_TTL_SECONDS,
            )
        except Exception:
            return

    def _load_state(self, now: float) -> TelegramBotGuardState:
        state = self._store.load()
        self._validate_state(state)
        self._prune(state, now)
        return state

    @staticmethod
    def _validate_state(state: Any) -> None:
        def valid_key(value: Any, size: int) -> bool:
            return (
                isinstance(value, tuple)
                and len(value) == size
                and all(isinstance(part, str) and bool(part) for part in value)
            )

        def valid_number(value: Any) -> bool:
            return type(value) in {int, float} and math.isfinite(value)

        def valid_windows(values: MutableMapping[Any, Any], key_size: int) -> bool:
            return all(
                valid_key(key, key_size)
                and isinstance(stamps, list)
                and all(valid_number(stamp) for stamp in stamps)
                for key, stamps in values.items()
            )

        if not isinstance(state, TelegramBotGuardState):
            raise TypeError("malformed Telegram bot guard state")
        mappings = (
            state.seen,
            state.message_depths,
            state.pair_windows,
            state.chat_windows,
            state.violations,
            state.breakers,
        )
        if not all(isinstance(value, MutableMapping) for value in mappings):
            raise TypeError("malformed Telegram bot guard state")
        if not all(
            valid_key(key, 4) and valid_number(expiry)
            for key, expiry in state.seen.items()
        ):
            raise TypeError("malformed Telegram bot guard state")
        if not all(
            valid_key(key, 2)
            and isinstance(value, tuple)
            and len(value) == 2
            and type(value[0]) is int
            and value[0] >= 0
            and valid_number(value[1])
            for key, value in state.message_depths.items()
        ):
            raise TypeError("malformed Telegram bot guard state")
        if not all((
            valid_windows(state.pair_windows, 4),
            valid_windows(state.chat_windows, 2),
            valid_windows(state.violations, 2),
        )):
            raise TypeError("malformed Telegram bot guard state")
        if not all(
            valid_key(key, 2) and valid_number(expiry)
            for key, expiry in state.breakers.items()
        ):
            raise TypeError("malformed Telegram bot guard state")

    @staticmethod
    def _prune(state: TelegramBotGuardState, now: float) -> None:
        state.seen = {key: expiry for key, expiry in state.seen.items() if expiry > now}
        state.message_depths = {
            key: value
            for key, value in state.message_depths.items()
            if isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], int)
            and isinstance(value[1], (int, float))
            and value[1] > now
        }
        cutoff = now - WINDOW_SECONDS
        state.pair_windows = {
            key: [stamp for stamp in stamps if stamp > cutoff]
            for key, stamps in state.pair_windows.items()
            if isinstance(stamps, list)
        }
        state.chat_windows = {
            key: [stamp for stamp in stamps if stamp > cutoff]
            for key, stamps in state.chat_windows.items()
            if isinstance(stamps, list)
        }
        state.violations = {
            key: [stamp for stamp in stamps if stamp > cutoff]
            for key, stamps in state.violations.items()
            if isinstance(stamps, list)
        }
        state.breakers = {
            key: expiry for key, expiry in state.breakers.items() if expiry > now
        }

    @staticmethod
    def _profile(event: Any) -> str:
        source = getattr(event, "source", None)
        return str(getattr(source, "profile", None) or "default").strip() or "default"

    @staticmethod
    def _required(value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("required Telegram bot guard identity is missing")
        return normalized

    @staticmethod
    def _message_text(event: Any) -> str:
        raw = getattr(event, "raw_message", None)
        return str(
            getattr(raw, "text", None)
            or getattr(raw, "caption", None)
            or getattr(event, "text", None)
            or ""
        )

    @classmethod
    def _is_command_mention(cls, event: Any, receiver_username: str) -> bool:
        return any(
            match.group(1).lower() == receiver_username
            for match in _COMMAND_TARGET_RE.finditer(cls._message_text(event))
        )

    @staticmethod
    def _is_direct_reply(raw_message: Any, receiver_bot_id: str) -> bool:
        reply = getattr(raw_message, "reply_to_message", None)
        author = getattr(reply, "from_user", None) if reply is not None else None
        return bool(
            reply is not None
            and author is not None
            and getattr(author, "is_bot", False)
            and str(getattr(author, "id", "")).strip() == receiver_bot_id
        )

    @classmethod
    def _addresses_any_bot(cls, content: str) -> bool:
        text = str(content or "")
        return bool(_COMMAND_TARGET_RE.search(text) or _BOT_HANDLE_RE.search(text))

    @staticmethod
    def _inbound_depth(
        state: TelegramBotGuardState,
        event: Any,
        *,
        chat_id: str,
        receiver_bot_id: str,
        allow_unreplied_root: bool,
    ) -> int | None:
        raw = getattr(event, "raw_message", None)
        reply = getattr(raw, "reply_to_message", None)
        if reply is None:
            return 1 if allow_unreplied_root else None

        reply_id = str(getattr(reply, "message_id", "") or "").strip()
        author = getattr(reply, "from_user", None)
        if not reply_id or author is None:
            return None

        author_id = str(getattr(author, "id", "") or "").strip()
        author_is_bot = bool(getattr(author, "is_bot", False))
        if not author_is_bot:
            return 1
        if author_id != receiver_bot_id:
            return None

        previous = state.message_depths.get((chat_id, reply_id))
        if previous is None:
            return None
        previous_depth = previous[0]
        if not isinstance(previous_depth, int):
            return None
        return previous_depth + 1

    def _depth_violation(
        self,
        state: TelegramBotGuardState,
        chat_key: tuple[str, str],
        now: float,
        reason: str,
        *,
        depth: int | None = None,
    ) -> TelegramBotGuardDecision:
        self._register_violation(state, chat_key, now)
        return self._decision(False, reason, depth=depth)

    def _limit_violation(
        self,
        state: TelegramBotGuardState,
        chat_key: tuple[str, str],
        now: float,
        reason: str,
    ) -> TelegramBotGuardDecision:
        self._register_violation(state, chat_key, now)
        return self._decision(False, reason)

    @staticmethod
    def _register_violation(
        state: TelegramBotGuardState,
        chat_key: tuple[str, str],
        now: float,
    ) -> None:
        timestamps = state.violations.setdefault(chat_key, [])
        timestamps.append(now)
        if len(timestamps) >= BREAKER_VIOLATIONS:
            state.breakers[chat_key] = now + BREAKER_SECONDS

    def _decision(
        self,
        allowed: bool,
        reason: str,
        *,
        depth: int | None = None,
    ) -> TelegramBotGuardDecision:
        counter = {
            "policy_none": "mention_drop",
            "unknown_lineage": "depth_drop",
        }.get(reason, reason)
        self._record(counter, allowed=allowed, reason=reason)
        return TelegramBotGuardDecision(allowed, reason, depth=depth)

    def _record(self, counter: str, *, allowed: bool, reason: str) -> None:
        self._counters[counter] += 1
        logger.info(
            "Telegram bot guard decision=%s reason=%s count=%d",
            "accept" if allowed else "drop",
            reason,
            self._counters[counter],
        )
