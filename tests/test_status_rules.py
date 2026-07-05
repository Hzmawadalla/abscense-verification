"""Locks the status classification policy (SPEC §4), incl. the exclusion decisions."""
import pytest

from ingestion.status_rules import classify


@pytest.mark.parametrize("raw,bucket", [
    # genuine unexplained absence
    ("Absent", "trigger"),
    ("No Show", "trigger"),
    # unconfirmed / failed leaves -> verify
    ("Annual Leave (Failed)", "trigger"),
    ("Annual Leave - To Be Confirmed", "trigger"),
    ("Sick Leave (Returned)", "trigger"),
    ("Bereavement Leave - To Be confirmed", "trigger"),
    # clean approved states -> skip
    ("Normal", "skip"),
    ("Weekend", "skip"),
    ("Annual Leave", "skip"),
    ("Unpaid Leave", "skip"),
    ("Sick", "skip"),                       # override shorthand
    # public holidays never reach a TL, even annotated
    ("Public Holiday", "skip"),
    ("Public Holiday - Check", "skip"),
    ("Public Holiday - To Be deducted from Balance", "skip"),
    # balance bookkeeping -> skip regardless of base
    ("Annual Leave to Be deducted", "skip"),
    # deducts but not verified this stage
    ("Late", "not_verified"),
    ("Missing Punch Out", "not_verified"),
    ("Half Day", "not_verified"),
    # known noise vs genuinely unknown
    ("#N/A", "ignore"),
    ("Departed", "ignore"),
    ("Some Brand New Status", "unknown"),
])
def test_bucket(raw, bucket):
    assert classify(raw)[0] == bucket


def test_half_day_flag():
    assert classify("Unpaid Leave (HD) - To Be Confirmed")[2] is True
    assert classify("Absent")[2] is False
