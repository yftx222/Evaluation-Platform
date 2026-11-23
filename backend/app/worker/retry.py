import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.contracts.runs import ExecutionPolicy


def retry_after(value: str | None, now: datetime) -> float | None:
    if not value or len(value) > 100:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return max(0.0, (stamp - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def delay(
    policy: ExecutionPolicy,
    *,
    stage: str,
    count: int,
    code: str,
    delivery: str,
    status: int | None,
    header: str | None,
    deadline: datetime,
    verified: bool,
) -> float | None:
    now = datetime.now(UTC)
    quota = code in {"TARGET_LIMIT", "JUDGE_LIMIT"}
    maximum = policy.adapter_max_attempts if stage == "ADAPTER" else policy.evaluator_max_attempts
    if (not quota and count >= maximum) or code not in policy.retryable_error_codes:
        return None
    if delivery != "not_sent" and status != 429:
        if not (
            verified
            and stage == "ADAPTER"
            and (delivery == "unknown" or (status is not None and 500 <= status <= 599))
        ):
            return None
    seconds = min(policy.max_backoff_ms, policy.initial_backoff_ms * 2 ** min(count - 1, 20)) / 1000
    if policy.jitter:
        seconds *= random.uniform(0.5, 1)
    hint = retry_after(header, now)
    if hint is not None:
        # Never retry earlier than the provider permits.
        if not quota and hint > policy.retry_after_cap_s:
            return None
        seconds = max(seconds, hint)
    return seconds if seconds < (deadline - now).total_seconds() else None
