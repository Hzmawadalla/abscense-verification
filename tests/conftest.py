"""Shared fixtures. Builds a synthetic workbook that mirrors the real HC + Structure tabs
(no PII), crafted to exercise every mapping edge: a TL, normal employees, a departed employee,
an employee with no Structure row, a 'boot camp' placeholder, and a TL missing from HC."""
import datetime
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HC_HEADERS = ["Type", "Vendor", "PS ID", "CRM", "Full Name", "Department",
              "Join Date\n(yyyy/mm/dd)", "Employee Status", "Exit Date\nyyyy/mm/dd",
              "Work Email address"]

HC_ROWS = [
    ["Full time", "JHR",     "1001", "TL-A", "Alice TL",  "EA", datetime.datetime(2024, 1, 1), "Active",   None,                          "alice@x.com"],
    ["Full time", "JHR",     "1002", "E-1",  "Emp One",   "EA", datetime.datetime(2024, 2, 1), "Active",   None,                          "e1@x.com"],
    ["Full time", "Migrate", "1003", "E-2",  "Emp Two",   "CC", datetime.datetime(2024, 3, 1), "Active",   None,                          "e2@x.com"],
    ["Full time", "JHR",     "1004", "E-3",  "Gone Away", "EA", datetime.datetime(2023, 1, 1), "Departed", datetime.datetime(2024, 6, 1), "e3@x.com"],
    ["Full time", "JHR",     "1005", "E-4",  "Orphan",    "EA", datetime.datetime(2024, 4, 1), "Active",   None,                          "e4@x.com"],
    ["Full time", "Migrate", "1006", "E-5",  "Fifth Emp", "CC", datetime.datetime(2024, 5, 1), "Active",   None,                          "e5@x.com"],
    ["Full time", "JHR",     "1099", "N/A",  "Junk Row",  "Admin", datetime.datetime(2024, 6, 1), "Active", None,                        "junk@x.com"],
]

STRUCT_HEADERS = ["SM/LTL/STL", "TL/Coach Lead", "EA 大组Bigteam", "Team", "CRM"]
STRUCT_ROWS = [
    ["SM-A", "TL-A", "BT1", "T1", "TL-A"],   # a TL is also a member of their own team
    ["SM-A", "TL-A", "BT1", "T1", "E-1"],
    ["SM-A", "TL-A", "BT1", "T1", "E-2"],
    ["SM-A", "TL-A", "BT1", "T1", "E-3"],     # departed — should be excluded from employees
    ["boot camp", "boot camp", "boot camp", "boot camp", "E-BC"],  # placeholder + not in HC
    ["SM-B", "TL-B", "BT2", "T2", "E-5"],     # TL-B is not in HC
    # E-4 intentionally absent from Structure -> unmapped
]


@pytest.fixture
def sample_workbook(tmp_path):
    wb = openpyxl.Workbook()
    hc = wb.active
    hc.title = "HC"
    hc.append(HC_HEADERS)
    for r in HC_ROWS:
        hc.append(r)
    st = wb.create_sheet("Structure")
    st.append(STRUCT_HEADERS)
    for r in STRUCT_ROWS:
        st.append(r)
    path = tmp_path / "sample.xlsx"
    wb.save(path)
    return str(path)
