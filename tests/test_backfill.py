"""Unit tests for the shared fragment-backfill helpers (resources/_backfill.py)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# _backfill is a sibling module of the zeeker resource files, which are loaded
# by file path (not as a package) — mirror the same sys.path shim here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
import _backfill  # noqa: E402

MAX_RETRIES = 3
TTL = _backfill.DEFAULT_RETRY_AFTER_SECONDS  # 14 days
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _record(failures, last_attempt=None):
    rec = {"failures": failures, "last_error": "RuntimeError: boom"}
    if last_attempt is not None:
        rec["last_attempt"] = last_attempt
    return rec


# ---------------------------------------------------------------------------
# Quarantine TTL eligibility
# ---------------------------------------------------------------------------


class TestIsEligible:
    def test_fresh_item_no_checkpoint_record(self):
        assert _backfill.is_eligible(None, MAX_RETRIES, NOW, TTL) is True

    def test_below_failure_cap_is_eligible(self):
        rec = _record(MAX_RETRIES - 1, NOW.isoformat())
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is True

    def test_quarantined_recent_not_eligible(self):
        last = (NOW - timedelta(days=1)).isoformat()
        rec = _record(MAX_RETRIES, last)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is False

    def test_quarantined_just_under_ttl_not_eligible(self):
        last = (NOW - timedelta(seconds=TTL - 1)).isoformat()
        rec = _record(MAX_RETRIES, last)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is False

    def test_quarantined_expired_is_eligible(self):
        last = (NOW - timedelta(seconds=TTL)).isoformat()
        rec = _record(MAX_RETRIES, last)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is True

    def test_quarantined_long_expired_is_eligible(self):
        last = (NOW - timedelta(days=90)).isoformat()
        rec = _record(MAX_RETRIES + 5, last)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is True

    def test_manual_flush_overrides_recent_quarantine(self):
        last = (NOW - timedelta(minutes=5)).isoformat()
        rec = _record(MAX_RETRIES, last)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL, retry_quarantined=True) is True

    def test_malformed_timestamp_fails_open(self):
        rec = _record(MAX_RETRIES, "not-a-timestamp")
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is True

    def test_missing_timestamp_fails_open(self):
        rec = _record(MAX_RETRIES)  # no last_attempt key at all
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is True

    def test_non_string_timestamp_fails_open(self):
        rec = _record(MAX_RETRIES, 12345)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is True

    def test_naive_timestamp_treated_as_utc(self):
        # Naive ISO timestamp (no offset) — must not crash, assumed UTC.
        last_naive = (NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
        rec = _record(MAX_RETRIES, last_naive)
        assert _backfill.is_eligible(rec, MAX_RETRIES, NOW, TTL) is False

    def test_failure_count_preserved_requarantines_after_one_more_failure(self):
        # An expired-quarantine item that fails once more is quarantined again:
        # its (preserved) failure count is already >= the cap, so with a fresh
        # last_attempt it is ineligible for another full TTL window.
        expired = _record(MAX_RETRIES, (NOW - timedelta(seconds=TTL + 60)).isoformat())
        assert _backfill.is_eligible(expired, MAX_RETRIES, NOW, TTL) is True
        refailed = _record(expired["failures"] + 1, NOW.isoformat())
        assert _backfill.is_eligible(refailed, MAX_RETRIES, NOW, TTL) is False


class TestIsQuarantined:
    def test_none_record(self):
        assert _backfill.is_quarantined(None, MAX_RETRIES) is False

    def test_empty_record(self):
        assert _backfill.is_quarantined({}, MAX_RETRIES) is False

    def test_below_cap(self):
        assert _backfill.is_quarantined(_record(MAX_RETRIES - 1), MAX_RETRIES) is False

    def test_at_cap(self):
        assert _backfill.is_quarantined(_record(MAX_RETRIES), MAX_RETRIES) is True


# ---------------------------------------------------------------------------
# Env-var readers
# ---------------------------------------------------------------------------


class TestEnvReaders:
    def test_retry_after_default_when_unset(self):
        assert _backfill.get_retry_after_seconds({}) == 1209600

    def test_retry_after_override(self):
        env = {_backfill.RETRY_AFTER_ENV: "3600"}
        assert _backfill.get_retry_after_seconds(env) == 3600

    def test_retry_after_garbage_falls_back(self):
        env = {_backfill.RETRY_AFTER_ENV: "two weeks"}
        assert _backfill.get_retry_after_seconds(env) == 1209600

    def test_retry_after_negative_falls_back(self):
        env = {_backfill.RETRY_AFTER_ENV: "-5"}
        assert _backfill.get_retry_after_seconds(env) == 1209600

    def test_retry_quarantined_default_off(self):
        assert _backfill.get_retry_quarantined({}) is False

    def test_retry_quarantined_on(self):
        env = {_backfill.RETRY_QUARANTINED_ENV: "1"}
        assert _backfill.get_retry_quarantined(env) is True

    def test_retry_quarantined_other_values_off(self):
        for value in ("0", "true", "yes", ""):
            assert (
                _backfill.get_retry_quarantined({_backfill.RETRY_QUARANTINED_ENV: value}) is False
            )


# ---------------------------------------------------------------------------
# Still-pending arithmetic
# ---------------------------------------------------------------------------


class TestComputeStillPending:
    def test_unattempted_plus_retryable_failures(self):
        # 20 candidates, batch of 10 attempted, 4 failed but not quarantined:
        # 10 unattempted + 4 retryable = 14 still pending.
        assert _backfill.compute_still_pending(20, 10, 4) == 14

    def test_all_attempted_all_succeeded(self):
        assert _backfill.compute_still_pending(5, 5, 0) == 0

    def test_all_attempted_some_retryable_failures(self):
        # Old arithmetic (total - batch) would report 0 here, hiding the 3
        # items that failed this batch but WILL be retried next build.
        assert _backfill.compute_still_pending(5, 5, 3) == 3

    def test_failures_all_quarantined_do_not_count(self):
        # Failed items that crossed the quarantine cap are not "pending".
        assert _backfill.compute_still_pending(10, 10, 0) == 0

    def test_no_candidates(self):
        assert _backfill.compute_still_pending(0, 0, 0) == 0


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


class TestFormatError:
    def test_includes_exception_class(self):
        err = ValueError("empty PDF text (12 chars)")
        assert _backfill.format_error(err) == "ValueError: empty PDF text (12 chars)"

    def test_truncates_message_never_class(self):
        err = ValueError("x" * 500)
        out = _backfill.format_error(err, max_message_len=80)
        assert out.startswith("ValueError: ")
        assert len(out) <= len("ValueError: ") + 80

    def test_empty_message_is_just_class(self):
        assert _backfill.format_error(KeyError()) == "KeyError"


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    def test_valid_utc(self):
        ts = _backfill.parse_timestamp("2026-07-01T00:00:00+00:00")
        assert ts == datetime(2026, 7, 1, tzinfo=timezone.utc)

    def test_naive_assumed_utc(self):
        ts = _backfill.parse_timestamp("2026-07-01T00:00:00")
        assert ts is not None and ts.tzinfo is not None

    def test_malformed_returns_none(self):
        assert _backfill.parse_timestamp("garbage") is None

    def test_none_returns_none(self):
        assert _backfill.parse_timestamp(None) is None

    def test_empty_returns_none(self):
        assert _backfill.parse_timestamp("") is None
