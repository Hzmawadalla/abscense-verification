"""resolve_sheet tolerates the tab-name quirks real HR exports ship with."""
import pytest

from ingestion.workbook import resolve_sheet


class FakeWB:
    def __init__(self, names):
        self.sheetnames = names


def test_exact_match_preferred():
    assert resolve_sheet(FakeWB(["HC", "Structure"]), "Structure") == "Structure"


def test_trailing_space_tolerated():
    assert resolve_sheet(FakeWB(["HC", "Structure "]), "Structure") == "Structure "


def test_case_insensitive():
    assert resolve_sheet(FakeWB(["hc", "STRUCTURE"]), "Structure") == "STRUCTURE"


def test_missing_sheet_raises_keyerror():
    with pytest.raises(KeyError):
        resolve_sheet(FakeWB(["HC"]), "Structure")
