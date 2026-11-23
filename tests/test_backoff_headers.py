"""Tests for backoff_headers."""

import random
from datetime import datetime, timezone

import pytest

from backoff_headers import (
    RateLimit,
    format_retry_after,
    parse_rate_limit,
    parse_retry_after,
    wait_time,
)

NOW = 1_700_000_000.0


class TestParseRetryAfter:
    def test_seconds(self):
        assert parse_retry_after("120") == 120.0

    def test_zero(self):
        assert parse_retry_after("0") == 0.0

    def test_surrounding_space(self):
        assert parse_retry_after("  30  ") == 30.0

    def test_negative_is_clamped_to_zero(self):
        assert parse_retry_after("-5") == 0.0

    def test_fractional_seconds_are_accepted(self):
        assert parse_retry_after("1.5") == 1.5

    def test_missing_value(self):
        assert parse_retry_after(None) is None

    def test_empty_value(self):
        assert parse_retry_after("   ") is None

    def test_http_date_in_the_future(self):
        moment = datetime.fromtimestamp(NOW + 90, tz=timezone.utc)
        assert parse_retry_after(moment.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=NOW) == 90.0

    def test_http_date_in_the_past_is_zero(self):
        moment = datetime.fromtimestamp(NOW - 90, tz=timezone.utc)
        assert parse_retry_after(moment.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=NOW) == 0.0

    def test_legacy_rfc850_date(self):
        moment = datetime.fromtimestamp(NOW + 60, tz=timezone.utc)
        assert parse_retry_after(moment.strftime("%A, %d-%b-%y %H:%M:%S GMT"), now=NOW) == 60.0

    def test_numeric_offset_date(self):
        moment = datetime.fromtimestamp(NOW + 30, tz=timezone.utc)
        assert parse_retry_after(moment.strftime("%a, %d %b %Y %H:%M:%S +0000"), now=NOW) == 30.0

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="not a Retry-After value"):
            parse_retry_after("soon")


class TestFormatRetryAfter:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0"), (0.1, "1"), (1, "1"), (1.4, "2"), (120, "120")],
    )
    def test_rounds_up(self, seconds, expected):
        assert format_retry_after(seconds) == expected

    def test_negative_becomes_zero(self):
        assert format_retry_after(-3) == "0"


class TestParseRateLimit:
    def test_rfc_headers(self):
        limit = parse_rate_limit(
            {"RateLimit-Limit": "100", "RateLimit-Remaining": "12", "RateLimit-Reset": "60"},
            now=NOW,
        )
        assert (limit.limit, limit.remaining, limit.reset) == (100, 12, NOW + 60)

    def test_github_headers_are_a_fallback(self):
        limit = parse_rate_limit(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(NOW) + 42),
            },
            now=NOW,
        )
        assert (limit.limit, limit.remaining) == (5000, 0)
        assert limit.seconds_until_reset(now=NOW) == 42.0

    def test_rfc_headers_win_over_github_ones(self):
        limit = parse_rate_limit(
            {"RateLimit-Limit": "10", "X-RateLimit-Limit": "5000"},
            now=NOW,
        )
        assert limit.limit == 10

    def test_header_names_are_case_insensitive(self):
        limit = parse_rate_limit({"ratelimit-limit": "7", "RATELIMIT-REMAINING": "1"}, now=NOW)
        assert (limit.limit, limit.remaining) == (7, 1)

    def test_a_large_reset_is_read_as_an_epoch(self):
        limit = parse_rate_limit({"RateLimit-Reset": str(int(NOW) + 30)}, now=NOW)
        assert limit.reset == int(NOW) + 30

    def test_epoch_style_can_be_forced(self):
        limit = parse_rate_limit({"RateLimit-Reset": "120"}, now=NOW, reset_style="epoch")
        assert limit.reset == 120

    def test_delta_style_can_be_forced(self):
        limit = parse_rate_limit({"RateLimit-Reset": "120"}, now=NOW, reset_style="delta")
        assert limit.reset == NOW + 120

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError, match="reset_style"):
            parse_rate_limit({}, reset_style="whenever")

    def test_no_headers_at_all(self):
        assert parse_rate_limit({}, now=NOW) is None

    def test_unrelated_headers(self):
        assert parse_rate_limit({"Content-Type": "application/json"}, now=NOW) is None

    def test_non_numeric_values_are_ignored(self):
        limit = parse_rate_limit({"RateLimit-Limit": "lots", "RateLimit-Remaining": "0"}, now=NOW)
        assert limit.limit is None
        assert limit.remaining == 0

    def test_policy_and_window(self):
        limit = parse_rate_limit(
            {"RateLimit-Policy": "100;w=60", "RateLimit-Limit": "100"}, now=NOW
        )
        assert (limit.policy, limit.window) == ("100;w=60", 60)

    def test_policy_without_a_window(self):
        limit = parse_rate_limit({"RateLimit-Policy": "100"}, now=NOW)
        assert limit.window is None

    def test_resource_and_used(self):
        limit = parse_rate_limit({"RateLimit-Resource": "search", "RateLimit-Used": "5"})
        assert (limit.resource, limit.used) == ("search", 5)

    def test_exhausted(self):
        assert parse_rate_limit({"RateLimit-Remaining": "0"}).exhausted
        assert not parse_rate_limit({"RateLimit-Remaining": "3"}).exhausted

    def test_seconds_until_reset_without_a_reset(self):
        assert RateLimit().seconds_until_reset(now=NOW) is None

    def test_seconds_until_reset_is_never_negative(self):
        assert RateLimit(reset=NOW - 10).seconds_until_reset(now=NOW) == 0.0


class TestWaitTime:
    def test_retry_after_wins(self):
        headers = {"Retry-After": "30", "RateLimit-Remaining": "0", "RateLimit-Reset": "600"}
        assert wait_time(headers, now=NOW) == 30.0

    def test_no_reason_to_wait(self):
        assert wait_time({"RateLimit-Remaining": "10", "RateLimit-Reset": "60"}, now=NOW) is None

    def test_default_is_used_when_nothing_says_to_wait(self):
        assert wait_time({}, now=NOW, default=5.0) == 5.0

    def test_exhausted_quota_waits_for_the_reset(self):
        headers = {"RateLimit-Remaining": "0", "RateLimit-Reset": "45"}
        assert wait_time(headers, now=NOW) == 45.0

    def test_github_reset_epoch_is_understood(self):
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW) + 20)}
        assert wait_time(headers, now=NOW) == 20.0

    def test_a_past_retry_after_date_means_wait_nothing(self):
        past = datetime.fromtimestamp(NOW - 60, tz=timezone.utc)
        headers = {"Retry-After": past.strftime("%a, %d %b %Y %H:%M:%S GMT")}
        assert wait_time(headers, now=NOW) == 0.0

    def test_cap_is_applied(self):
        assert wait_time({"Retry-After": "600"}, now=NOW, cap=30) == 30.0

    def test_jitter_is_added_within_the_bound(self):
        headers = {"Retry-After": "10"}
        result = wait_time(headers, now=NOW, jitter=5, rng=random.Random(0))
        assert 10.0 <= result < 15.0

    def test_jitter_is_reproducible_with_a_seeded_generator(self):
        first = wait_time({"Retry-After": "10"}, now=NOW, jitter=5, rng=random.Random(7))
        second = wait_time({"Retry-After": "10"}, now=NOW, jitter=5, rng=random.Random(7))
        assert first == second

    def test_negative_jitter_raises(self):
        with pytest.raises(ValueError, match="jitter"):
            wait_time({}, jitter=-1)

    def test_negative_cap_raises(self):
        with pytest.raises(ValueError, match="cap"):
            wait_time({}, cap=-1)

    def test_cap_and_jitter_are_rounded(self):
        result = wait_time({"Retry-After": "1"}, now=NOW, jitter=0.5, rng=random.Random(1))
        assert result == round(result, 3)


class TestRigour:
    """Header shapes that the first pass did not reach."""

    def test_a_date_without_a_timezone(self):
        import time

        future = time.localtime(time.time() + 120)
        stamp = time.strftime("%a %b %e %H:%M:%S %Y", future)
        assert parse_retry_after(stamp) > 0

    def test_a_reset_value_that_is_not_a_number_is_ignored(self):
        limit = parse_rate_limit({"RateLimit-Reset": "soon"})
        assert limit.reset is None

    def test_the_clock_defaults_to_now(self):
        assert wait_time({"Retry-After": "0"}) == 0.0

    def test_a_reset_far_in_the_past_waits_nothing(self):
        # RFC 9331 counts seconds from now, GitHub sends an absolute epoch. Reading one
        # as the other is the bug this library exists for.
        assert wait_time({"RateLimit-Reset": "1", "RateLimit-Remaining": "0"}, now=5) == 1.0
        assert wait_time(
            {"X-RateLimit-Reset": "1700000000", "X-RateLimit-Remaining": "0"}, now=1_699_999_900
        ) == 100.0
