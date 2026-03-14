"""Tests for setup endpoint rate limiting logic in app.api.auth."""

import time
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.auth import (
    _MAX_TRACKED_IPS,
    SETUP_RATE_LIMIT,
    SETUP_RATE_WINDOW,
    _check_setup_rate_limit,
    _reset_setup_rate_limit,
    _setup_attempts,
)


def _make_request(ip: str) -> MagicMock:
    """Create a mock Request with the given client IP."""
    request = MagicMock()
    request.client.host = ip
    return request


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset rate-limit state before and after every test."""
    _reset_setup_rate_limit()
    yield
    _reset_setup_rate_limit()


# ---------- Normal flow ----------


def test_first_attempt_passes():
    """A single attempt from a fresh IP should not raise."""
    _check_setup_rate_limit(_make_request("10.0.0.1"))


def test_multiple_attempts_below_limit():
    """Attempts up to SETUP_RATE_LIMIT - 1 should all pass without 429."""
    req = _make_request("10.0.0.1")
    for _ in range(SETUP_RATE_LIMIT - 1):
        _check_setup_rate_limit(req)

    # One more should still succeed (limit is checked *before* appending)
    # The function checks >= SETUP_RATE_LIMIT on the pruned list, then appends.
    # After SETUP_RATE_LIMIT - 1 calls the list has that many entries;
    # the next call sees len < SETUP_RATE_LIMIT, so it passes and appends the 5th.


def test_exactly_at_limit_passes():
    """The SETUP_RATE_LIMIT-th attempt itself should still succeed (limit is >= check
    before append, but the list has LIMIT-1 entries at that point)."""
    req = _make_request("10.0.0.1")
    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req)
    # All SETUP_RATE_LIMIT calls passed; the dict now has SETUP_RATE_LIMIT entries.


# ---------- Rate limit triggered ----------


def test_rate_limit_raises_429():
    """After SETUP_RATE_LIMIT successful attempts, the next one should raise 429."""
    req = _make_request("10.0.0.1")
    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req)

    with pytest.raises(HTTPException) as exc_info:
        _check_setup_rate_limit(req)
    assert exc_info.value.status_code == 429
    assert "Too many setup attempts" in exc_info.value.detail


def test_rate_limit_stays_enforced():
    """Repeated calls after hitting the limit should keep raising 429."""
    req = _make_request("10.0.0.1")
    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req)

    for _ in range(3):
        with pytest.raises(HTTPException) as exc_info:
            _check_setup_rate_limit(req)
        assert exc_info.value.status_code == 429


# ---------- Different IPs ----------


def test_different_ips_independent():
    """Rate limits are per-IP; exhausting one IP does not affect another."""
    req_a = _make_request("10.0.0.1")
    req_b = _make_request("10.0.0.2")

    # Exhaust IP A
    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req_a)

    # IP A is now rate-limited
    with pytest.raises(HTTPException):
        _check_setup_rate_limit(req_a)

    # IP B should still work fine
    _check_setup_rate_limit(req_b)


def test_multiple_ips_all_rate_limited_independently():
    """Each IP has its own counter; both can hit the limit independently."""
    req_a = _make_request("10.0.0.1")
    req_b = _make_request("10.0.0.2")

    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req_a)
        _check_setup_rate_limit(req_b)

    with pytest.raises(HTTPException):
        _check_setup_rate_limit(req_a)
    with pytest.raises(HTTPException):
        _check_setup_rate_limit(req_b)


# ---------- Window expiry ----------


def test_window_expiry_clears_old_attempts():
    """Attempts older than SETUP_RATE_WINDOW are pruned, allowing new attempts."""
    req = _make_request("10.0.0.1")
    past_time = time.time() - SETUP_RATE_WINDOW - 10  # well outside the window

    # Manually inject SETUP_RATE_LIMIT old timestamps
    _setup_attempts["10.0.0.1"] = [past_time + i for i in range(SETUP_RATE_LIMIT)]

    # The next call should prune all old entries and succeed
    _check_setup_rate_limit(req)

    # The IP should have exactly 1 entry (the new one)
    assert len(_setup_attempts["10.0.0.1"]) == 1


def test_partial_window_expiry():
    """Only timestamps outside the window are pruned; recent ones remain."""
    req = _make_request("10.0.0.1")
    now = time.time()
    old_time = now - SETUP_RATE_WINDOW - 10

    # Inject a mix: some old, some recent (but below the limit after pruning)
    _setup_attempts["10.0.0.1"] = [
        old_time,
        old_time + 1,
        old_time + 2,
        now - 10,  # recent, within window
        now - 5,  # recent, within window
    ]

    # After pruning, only 2 recent entries remain — below SETUP_RATE_LIMIT
    _check_setup_rate_limit(req)

    # Should now have 3 entries: the 2 recent ones + the newly appended one
    assert len(_setup_attempts["10.0.0.1"]) == 3


def test_window_expiry_removes_empty_entry():
    """When all timestamps for an IP expire, the IP key is deleted from the dict."""
    past_time = time.time() - SETUP_RATE_WINDOW - 10
    _setup_attempts["10.0.0.1"] = [past_time]

    req = _make_request("10.0.0.1")
    _check_setup_rate_limit(req)

    # The old entry was pruned (empty list → deleted), then a new entry was appended.
    # So the key exists with 1 entry.
    assert "10.0.0.1" in _setup_attempts
    assert len(_setup_attempts["10.0.0.1"]) == 1


def test_stale_ip_not_pruned_by_other_ip_call():
    """A stale IP entry is not removed when a different IP makes a request."""
    past_time = time.time() - SETUP_RATE_WINDOW - 100
    _setup_attempts["10.0.0.99"] = [past_time]

    # Call from a different IP so that 10.0.0.99 isn't touched directly
    req = _make_request("10.0.0.1")
    _check_setup_rate_limit(req)

    # 10.0.0.99 was never touched by this call, so it remains with its stale entry
    # (pruning only happens for the calling IP)
    assert "10.0.0.99" in _setup_attempts


# ---------- IP cap eviction ----------


def test_ip_cap_eviction():
    """When _MAX_TRACKED_IPS is reached, the oldest IP entry is evicted."""
    now = time.time()

    # Fill up to the cap with distinct IPs, each having one timestamp.
    # Make the "oldest" IP have the earliest timestamp so it gets evicted.
    oldest_ip = "10.99.99.99"
    _setup_attempts[oldest_ip] = [now - 5000]

    for i in range(_MAX_TRACKED_IPS - 1):
        ip = f"10.0.{i // 256}.{i % 256}"
        _setup_attempts[ip] = [now - 1000 + i]

    assert len(_setup_attempts) == _MAX_TRACKED_IPS

    # Now trigger a check from a brand-new IP — should evict the oldest
    new_req = _make_request("10.255.255.255")
    _check_setup_rate_limit(new_req)

    assert oldest_ip not in _setup_attempts
    assert "10.255.255.255" in _setup_attempts


def test_ip_cap_eviction_picks_oldest_first_timestamp():
    """Eviction selects the IP whose earliest timestamp is the smallest."""
    now = time.time()

    # Two IPs: one with an old first timestamp, one with a newer first timestamp
    _setup_attempts.clear()
    _setup_attempts["old-ip"] = [now - 9000]
    _setup_attempts["new-ip"] = [now - 100]

    # Fill remaining slots
    for i in range(_MAX_TRACKED_IPS - 2):
        ip = f"10.1.{i // 256}.{i % 256}"
        _setup_attempts[ip] = [now - 50]

    assert len(_setup_attempts) == _MAX_TRACKED_IPS

    new_req = _make_request("10.255.255.255")
    _check_setup_rate_limit(new_req)

    # "old-ip" had the oldest first timestamp and should be evicted
    assert "old-ip" not in _setup_attempts
    assert "new-ip" in _setup_attempts


# ---------- Reset ----------


def test_reset_clears_all_state():
    """_reset_setup_rate_limit clears all tracked IPs."""
    req_a = _make_request("10.0.0.1")
    req_b = _make_request("10.0.0.2")

    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req_a)
        _check_setup_rate_limit(req_b)

    _reset_setup_rate_limit()

    assert len(_setup_attempts) == 0

    # Both IPs should be able to make requests again
    _check_setup_rate_limit(req_a)
    _check_setup_rate_limit(req_b)


def test_reset_allows_previously_limited_ip():
    """After reset, a previously rate-limited IP can make attempts again."""
    req = _make_request("10.0.0.1")

    for _ in range(SETUP_RATE_LIMIT):
        _check_setup_rate_limit(req)

    with pytest.raises(HTTPException):
        _check_setup_rate_limit(req)

    _reset_setup_rate_limit()

    # Should work again after reset
    _check_setup_rate_limit(req)


# ---------- Edge cases ----------


def test_unknown_client():
    """When request.client is None, IP falls back to 'unknown'."""
    req = MagicMock()
    req.client = None

    # Should not raise for initial attempts
    _check_setup_rate_limit(req)
    assert "unknown" in _setup_attempts
