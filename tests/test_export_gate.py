"""The Export tab's acknowledgement gate (app.export_gate): when the tick is required, that the
download stays disabled until it is ticked, and that every rebuild or file/year change resets it.
The Streamlit page itself can't be driven here (AppTest can't upload a file); see the PR notes."""
from types import SimpleNamespace

import pytest

from app import export_gate as g


def _result(unresolved=0, not_in_workbook=0, blank_cells=0):
    return SimpleNamespace(unresolved=unresolved, not_in_workbook=not_in_workbook,
                           blank_cells=blank_cells)


@pytest.mark.parametrize("res,needed", [
    (_result(), False),
    (_result(unresolved=1), True),
    (_result(not_in_workbook=2), True),
    (_result(blank_cells=40), False),          # blanks are information only
])
def test_tick_is_required_only_for_unresolved_or_missing_rows(res, needed):
    assert g.needs_ack(res) is needed


def test_download_waits_for_the_tick_when_required():
    res = _result(unresolved=3)
    assert g.download_enabled(res, False) is False
    assert g.download_enabled(res, True) is True


def test_download_is_open_when_nothing_needs_acknowledging():
    assert g.download_enabled(_result(blank_cells=5), False) is True


def test_rebuild_drops_the_report_and_the_tick():
    state = {g.BUILT_KEY: {"key": ("h", 2026)}, g.ACK_KEY: True, "other": 1}
    g.reset(state)
    assert state == {"other": 1}


def test_same_file_and_year_keep_the_report_and_the_tick():
    built = {"key": ("h", 2026), "result": _result(unresolved=1)}
    state = {g.BUILT_KEY: built, g.ACK_KEY: True}
    assert g.current(state, ("h", 2026)) is built
    assert state[g.ACK_KEY] is True


@pytest.mark.parametrize("key", [("other-hash", 2026), ("h", 2027), None])
def test_a_changed_file_or_year_drops_the_stale_report_and_its_tick(key):
    state = {g.BUILT_KEY: {"key": ("h", 2026)}, g.ACK_KEY: True}
    assert g.current(state, key) is None
    assert g.BUILT_KEY not in state and g.ACK_KEY not in state


def test_nothing_built_yet():
    state = {}
    assert g.current(state, ("h", 2026)) is None and state == {}
