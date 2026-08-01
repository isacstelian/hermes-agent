# Implementation Notes — Trusted Telegram Group Adder

## Goal
Allow a Telegram bot to auto-authorize a group only when Isac promotes the bot to administrator, while keeping mention/reply-only responses and full passive context observation.

## Major Decisions I Made Because the Spec Was Missing Context

### Require an administrator promotion, not mere membership
I chose to enroll only when the bot transitions to Telegram `administrator` and the actor is an allowlisted trusted user. This matches Isac's actual workflow and avoids silently trusting groups where the bot was merely added.

### Persist enrollment outside config.yaml
I chose a profile-local state file written atomically with restrictive permissions so runtime enrollment does not rewrite config files or expose secrets. Removing/kicking the bot must revoke the saved enrollment so an untrusted user cannot re-add it later and inherit old access.

### Fail closed
If config is absent/invalid, the actor is not trusted, the chat is not a group, or state cannot be validated, do not enroll. When trusted-adder mode is enabled, I also made group admission strict even when every allowlist is empty. `group_allow_from` and `guest_mode` cannot admit an unknown group; either explicit chat allowlist can admit it, and normal mention/reply gating still runs afterward.

### Expose dynamic enrollment without rewriting config
I chose to have gateway authorization read the Telegram adapter's effective group allowlist helper. This makes persisted groups visible to anonymous/shared group sources without mutating or removing the operator's explicit `group_allowed_chats` config.

### Revoke on every administrator demotion
I chose to revoke persisted enrollment whenever the bot transitions from `administrator` to any other status, regardless of the actor. Only enrollment requires a trusted actor; revocation must fail safe.

## Verification
- `uv run pytest -q tests/gateway/test_telegram_trusted_group_adder.py`: 18 passed.
- `uv run pytest -q tests/gateway/test_telegram_group_gating.py`: 71 passed.
- Relevant gateway authorization suites (`test_telegram_auth_check.py`, `test_config_driven_access_policy.py`, `test_multiplex_profile_authz.py`, `test_auth_fallback.py`): 105 passed.
- `uv run python -m py_compile` on all changed Python files: passed.
- `uv run ruff check` on all changed Python files: passed.
- `git diff --check`: passed.

## Follow-ups / Open Questions
- After merge, enable the feature only on Telegram profiles with Isac's current Telegram user ID and restart those gateways.
- Smoke-test first on one non-production bot/group before fleet rollout.
- **Deployment blocker:** the active `scoped-telegram-access` plugins for the `it-admin` and `junior-dev` profiles still deny ordinary members in newly auto-enrolled groups unless the chat/user is also present in each plugin's static `OPEN_GROUPS`, `GLOBAL_GROUP_ACCESS`, or `SCOPED_ACCESS`. Do not enable this feature for those profiles until that policy is deliberately reconciled. The profile plugins were inspected read-only and were not edited in this branch.
