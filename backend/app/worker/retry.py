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


QUOTA_ERRORS = {"TARGET_LIMIT", "JUDGE_LIMIT"}


def quota_delay(
    policy: ExecutionPolicy, count: int, available_in: float | None, deadline: datetime
) -> float | None:
    # Local admission has its own clock and minimum interval, including when a
    # task chooses millisecond-scale retry settings for external calls.
    floor = max(0.25, policy.initial_backoff_ms / 1000)
    if available_in is not None:
        seconds = max(0.25, available_in)
    else:
        ceiling = max(floor, policy.max_backoff_ms / 1000)
        seconds = min(ceiling, floor * 2 ** min(max(count - 1, 0), 20))
        if policy.jitter:
            seconds = random.uniform(max(floor, seconds / 2), seconds)
    return seconds if seconds < (deadline - datetime.now(UTC)).total_seconds() else None


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
    maximum = policy.adapter_max_attempts if stage == "ADAPTER" else policy.evaluator_max_attempts
    if code in QUOTA_ERRORS or count >= maximum or code not in policy.retryable_error_codes:
        return None
    if delivery != "not_sent" and status != 429:
        if not (
            verified
            and stage == "ADAPTER"
            and (delivery == "unknown" or (status is not None and 500 <= status <= 599))
        ):
            return None
    seconds = (
        min(policy.max_backoff_ms, policy.initial_backoff_ms * 2 ** min(max(count - 1, 0), 20))
        / 1000
    )
    if policy.jitter:
        seconds *= random.uniform(0.5, 1)
    hint = retry_after(header, now)
    if hint is not None:
        # Never retry earlier than the provider permits.
        if hint > policy.retry_after_cap_s:
            return None
        seconds = max(seconds, hint)
    return seconds if seconds < (deadline - now).total_seconds() else None
