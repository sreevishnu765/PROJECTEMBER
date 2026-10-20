"""Calendar dates ("31st of January 2027") and an ID-free reminder list."""
from datetime import datetime
from ember_reminders import parse_natural_time

NOW = datetime(2026, 10, 20, 12, 0)


def t(p):
    return parse_natural_time(p, now=NOW)


def test_day_first_with_year():
    assert t("for the 31st of January 2027")[0] == datetime(2027, 1, 31, 9, 0)


def test_garbled_year_after_month_resolves_to_next_occurrence():
    assert t("for the 31st of January 20, 2017.")[0] == datetime(2027, 1, 31, 9, 0)


def test_month_first_with_time():
    assert t("on January 31, 2027 at 5pm")[0] == datetime(2027, 1, 31, 17, 0)


def test_no_year_rolls_forward():
    assert t("on 5 march")[0] == datetime(2027, 3, 5, 9, 0)
    assert t("on 25 december at 6pm")[0] == datetime(2026, 12, 25, 18, 0)


def test_numeric_and_iso_and_ordinal_only():
    assert t("on 31/01/2027")[0] == datetime(2027, 1, 31, 9, 0)
    assert t("on 2027-01-31")[0] == datetime(2027, 1, 31, 9, 0)
    assert t("on the 31st")[0] == datetime(2026, 10, 31, 9, 0)


def test_explicit_past_year_refused_with_suggestion():
    dt, err = t("on 31 January 2017")
    assert dt is None and "already passed" in err and "31 January 2027" in err


def test_invalid_date():
    assert t("on 30 february 2028")[0] is None


def test_existing_phrases_unchanged():
    assert t("tomorrow at 5pm")[0] == datetime(2026, 10, 21, 17, 0)
    assert t("at 4pm")[0] == datetime(2026, 10, 20, 16, 0)
    assert t("in 2 hours")[0] == datetime(2026, 10, 20, 14, 0)
    assert t("wednesday")[0] == datetime(2026, 10, 21, 9, 0)
    assert t("2026-10-25 08:30")[0] == datetime(2026, 10, 25, 8, 30)
