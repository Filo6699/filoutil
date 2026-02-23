from datetime import datetime, timezone

from filoutil.monitor.engine import (
    SSL_EXPIRY_ALERT_THRESHOLDS,
    _should_send_ssl_alert,
    _ssl_days_left,
)


def test_ssl_days_left_uses_calendar_days_utc():
    now = datetime(2026, 2, 23, 23, 30, tzinfo=timezone.utc)
    expiry = datetime(2026, 2, 24, 0, 10, tzinfo=timezone.utc)
    assert _ssl_days_left(expiry, now) == 1


def test_first_run_only_alerts_on_threshold():
    assert _should_send_ssl_alert(5, None, SSL_EXPIRY_ALERT_THRESHOLDS)
    assert not _should_send_ssl_alert(4, None, SSL_EXPIRY_ALERT_THRESHOLDS)


def test_same_day_does_not_repeat_alert():
    assert not _should_send_ssl_alert(5, 5, SSL_EXPIRY_ALERT_THRESHOLDS)


def test_crossing_threshold_triggers_once():
    assert _should_send_ssl_alert(5, 6, SSL_EXPIRY_ALERT_THRESHOLDS)


def test_skipping_days_still_triggers_crossed_threshold():
    assert _should_send_ssl_alert(4, 6, SSL_EXPIRY_ALERT_THRESHOLDS)


def test_requested_final_countdown_thresholds():
    for days in [5, 3, 2, 1, 0]:
        assert _should_send_ssl_alert(days, days + 1, SSL_EXPIRY_ALERT_THRESHOLDS)


def test_expired_state_maps_to_zero_day_threshold():
    assert _should_send_ssl_alert(-2, None, SSL_EXPIRY_ALERT_THRESHOLDS)
    assert _should_send_ssl_alert(-1, 1, SSL_EXPIRY_ALERT_THRESHOLDS)
    assert not _should_send_ssl_alert(-3, -2, SSL_EXPIRY_ALERT_THRESHOLDS)
