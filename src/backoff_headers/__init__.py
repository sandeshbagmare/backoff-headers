"""Read ``Retry-After`` and rate-limit headers, and turn them into a wait time.

    >>> from backoff_headers import wait_time
    >>> wait_time({"Retry-After": "30"}, now=0)
    30.0

Two headers carry the same idea and disagree about units. ``Retry-After`` is either a
number of seconds or an HTTP date. ``RateLimit-Reset`` (RFC 9331) is seconds from now,
while the older ``X-RateLimit-Reset`` that GitHub made popular is an absolute epoch.
Dividing by the wrong one is how a client ends up waiting until 1970 or for eleven
days. This module reads both, in either style, and hands back seconds to wait.

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

__all__ = [
    "RateLimit",
    "format_retry_after",
    "parse_rate_limit",
    "parse_retry_after",
    "wait_time",
]

__version__ = "0.1.0"

_NUMBER = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*$")
_WINDOW = re.compile(r";\s*w\s*=\s*(\d+)")
# Anything larger than this is not "seconds from now" but a Unix timestamp: a delta of
# eleven million seconds is four months, and no server sends that.
_EPOCH_FLOOR = 10_000_000


def _get(headers: Mapping[str, str], *names: str) -> str | None:
    """Case-insensitive header lookup, trying the names in order."""
    lowered = {key.lower(): value for key, value in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return None


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` value.

    Accepts the two forms the header allows: delay-seconds (``120``) and an HTTP date
    (``Wed, 21 Oct 2015 07:28:00 GMT``). A date in the past gives ``0.0``. ``None`` and
    an empty value give ``None``; anything else raises :class:`ValueError`.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _NUMBER.match(text):
        return max(0.0, float(text))
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        raise ValueError(f"not a Retry-After value: {value!r}") from None
    if moment is None:  # pragma: no cover - defensive, parsedate returns None rarely
        raise ValueError(f"not a Retry-After value: {value!r}")
    if moment.tzinfo is None:
        stamp = moment.timestamp()
    else:
        import datetime as _dt

        stamp = moment.astimezone(_dt.timezone.utc).timestamp()
    reference = _now(now)
    return max(0.0, round(stamp - reference, 3))


def format_retry_after(seconds: float) -> str:
    """The header value for a wait, rounded up so a client never retries early."""
    return str(max(0, math.ceil(seconds)))


@dataclass(frozen=True)
class RateLimit:
    """What the rate-limit headers of one response say."""

    limit: int | None = None
    remaining: int | None = None
    reset: float | None = None
    used: int | None = None
    resource: str | None = None
    policy: str | None = None
    window: int | None = None

    @property
    def exhausted(self) -> bool:
        """True when the quota is used up."""
        return self.remaining == 0

    def seconds_until_reset(self, now: float | None = None) -> float | None:
        if self.reset is None:
            return None
        return max(0.0, round(self.reset - _now(now), 3))


def _now(now: float | None) -> float:
    if now is not None:
        return float(now)
    import time

    return time.time()


def parse_rate_limit(
    headers: Mapping[str, str],
    *,
    now: float | None = None,
    reset_style: str = "auto",
) -> RateLimit | None:
    """Read the rate-limit headers of a response, or ``None`` if there are none.

    ``RateLimit-*`` (RFC 9331) wins over ``X-RateLimit-*``. ``reset_style`` decides how
    to read the reset field: ``"delta"`` seconds from now, ``"epoch"`` a Unix timestamp,
    or ``"auto"`` (default) which treats an implausibly large number as a timestamp.
    """
    if reset_style not in ("auto", "delta", "epoch"):
        raise ValueError(f"reset_style must be auto, delta or epoch, not {reset_style!r}")

    limit = _int(_get(headers, "RateLimit-Limit", "X-RateLimit-Limit"))
    remaining = _int(_get(headers, "RateLimit-Remaining", "X-RateLimit-Remaining"))
    raw_reset = _get(headers, "RateLimit-Reset", "X-RateLimit-Reset")
    used = _int(_get(headers, "RateLimit-Used"))
    resource = _get(headers, "RateLimit-Resource", "X-RateLimit-Resource")
    policy = _get(headers, "RateLimit-Policy")
    window = None
    if policy:
        match = _WINDOW.search(policy)
        window = int(match.group(1)) if match else None

    if (
        limit is None
        and remaining is None
        and raw_reset is None
        and used is None
        and policy is None
        and resource is None
    ):
        return None

    reset: float | None = None
    if raw_reset is not None:
        try:
            number = float(raw_reset.strip())
        except ValueError:
            number = None
        if number is not None:
            style = reset_style
            if style == "auto":
                style = "epoch" if number >= _EPOCH_FLOOR else "delta"
            reset = number if style == "epoch" else _now(now) + number

    return RateLimit(
        limit=limit,
        remaining=remaining,
        reset=reset,
        used=used,
        resource=resource,
        policy=policy,
        window=window,
    )


def wait_time(
    headers: Mapping[str, str],
    *,
    now: float | None = None,
    default: float | None = None,
    cap: float | None = None,
    jitter: float = 0.0,
    rng: random.Random | None = None,
) -> float | None:
    """Seconds to wait before the next request, or ``default`` when there is no reason.

    ``Retry-After`` wins over the rate-limit headers. When the quota is exhausted the
    wait is the time until the reset; while requests remain there is nothing to wait
    for and ``default`` (``None`` unless given) comes back. ``jitter`` adds a random
    spread in ``[0, jitter)`` so many clients do not retry in lockstep, and ``cap``
    limits the result.
    """
    if jitter < 0:
        raise ValueError("jitter must not be negative")
    if cap is not None and cap < 0:
        raise ValueError("cap must not be negative")

    wait = parse_retry_after(_get(headers, "Retry-After"), now=now)
    if wait is None:
        limit = parse_rate_limit(headers, now=now)
        if limit is not None and limit.exhausted:
            wait = limit.seconds_until_reset(now)
    if wait is None:
        return default

    if jitter:
        generator = rng or random.Random()
        wait += generator.uniform(0.0, jitter)
    if cap is not None:
        wait = min(wait, float(cap))
    return round(max(0.0, wait), 3)
