from filoutil.session_refresh.engine import _oidc_retry_delay_seconds


def test_oidc_retry_delay_schedule_minutes():
    expected_minutes = {
        1: 15,
        2: 30,
        3: 60,
        4: 90,
        5: 120,
        6: 150,
        7: 180,
        8: 210,
        9: 240,
        10: 1440,
        11: 1440,
        25: 1440,
    }

    for retry_count, expected in expected_minutes.items():
        assert _oidc_retry_delay_seconds(retry_count) == expected * 60
