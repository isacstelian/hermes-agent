# Implementation Notes

## Goal
Add a backward-compatible Telegram option that shows 👀 while Hermes processes a message, then clears the reaction after completion without adding 👍/👎.

## Major Decisions I Made Because the Spec Was Missing Context

### Keep existing behavior as the default
I chose an opt-in `telegram.remove_reaction_after_completion` boolean because existing users who enable `telegram.reactions` currently expect terminal 👍/👎 reactions.

The other reasonable option was to change the default globally, but that would silently alter every Telegram profile. If this naming should change, update the Telegram plugin config bridge, docs, and tests together.

### Clear on every terminal outcome
I chose to clear 👀 after success, failure, and cancellation when the option is enabled because Isac explicitly requested no final like/dislike reaction.

The other reasonable option was to preserve 👎 on failure, but that contradicts “do not react after finishing.”

### Reuse the existing Telegram reaction bridge
I chose to bridge `telegram.remove_reaction_after_completion` to `TELEGRAM_REMOVE_REACTION_AFTER_COMPLETION` in the bundled Telegram plugin, beside the existing `telegram.reactions` bridge. This keeps YAML configuration, direct environment overrides, and runtime checks consistent.

The environment variable takes precedence when it is already non-empty, matching the existing reaction option.

## Verification
- RED: `python -m pytest tests/gateway/test_telegram_reactions.py -o 'addopts=' -q` → `3 failed, 23 passed`; success/failure still sent 👍/👎 and the YAML option was not bridged.
- GREEN: the same focused command → `26 passed in 1.19s`.
- Relevant regression: `python -m pytest tests/gateway/test_telegram_reactions.py tests/gateway/test_platform_registry.py tests/test_gateway_streaming_nested_config.py -o 'addopts=' -q` → `92 passed in 3.78s`.
- Static checks: `git diff --check` and `python -m py_compile plugins/platforms/telegram/adapter.py` passed.

## Follow-ups / Open Questions
- None. No gateway or profile configuration was changed or restarted.
