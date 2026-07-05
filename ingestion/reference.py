"""Parse the HC + Structure tabs into managers, employees, and a mapping between them (SPEC §6.1).

Pure function over a workbook path — no database. Returns structured data the seed step upserts,
and an exceptions list for anything that can't be cleanly mapped (never silently dropped).

CRM is the join key across tabs but its casing is inconsistent between HC and Structure, so all
matching is case-insensitive on a lowercased key; the canonical display casing comes from HC."""
from dataclasses import dataclass, field

from .workbook import pick, read_dicts, to_date

PLACEHOLDERS = {"boot camp"}
_JUNK_CRM = {"n/a", "#n/a", "#ref!", "0", "na", "none"}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _key(crm):
    """Case-insensitive join key for a cleaned CRM."""
    return crm.lower() if crm else None


def _is_junk_crm(crm) -> bool:
    """Reject malformed CRM keys (blank, N/A, numeric-only, team codes, formula errors)."""
    if crm is None:
        return True
    s = str(crm).strip().lower()
    if s in _JUNK_CRM or s == "":
        return True
    if s.replace(".", "", 1).isdigit():
        return True
    if "大组" in s or "#ref" in s or "#n/a" in s:
        return True
    return False


@dataclass
class Manager:
    crm: str
    name: str | None = None
    email: str | None = None
    active: bool = True


@dataclass
class Employee:
    crm: str
    ps_id: str | None = None
    name: str | None = None
    email: str | None = None
    department: str | None = None
    vendor: str | None = None
    employee_status: str | None = None
    join_date: object = None
    exit_date: object = None
    manager_crm: str | None = None
    sm_crm: str | None = None
    team: str | None = None


@dataclass
class RefException:
    crm: str | None
    reason: str
    detail: str | None = None


@dataclass
class ReferenceData:
    managers: list = field(default_factory=list)
    employees: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _structure_map(struct_rows):
    """employee_key -> {tl_key, tl_raw, sm_raw, team, emp_raw}."""
    mapping = {}
    for row in struct_rows:
        ecrm = _clean(pick(row, "crm"))
        if not ecrm or _is_junk_crm(ecrm):
            continue
        tl = _clean(pick(row, "tl/coach lead", "tl"))
        if tl and (tl.lower() in PLACEHOLDERS or _is_junk_crm(tl)):
            tl = None
        sm = _clean(pick(row, "sm/ltl/stl", "sm"))
        if sm and (sm.lower() in PLACEHOLDERS or _is_junk_crm(sm)):
            sm = None
        mapping[_key(ecrm)] = {
            "tl_key": _key(tl), "tl_raw": tl,
            "sm_raw": sm, "team": _clean(pick(row, "team")), "emp_raw": ecrm,
        }
    return mapping


def parse_reference(path, hc_sheet="HC", structure_sheet="Structure", aliases=None):
    """aliases: optional {lowercased nickname/crm -> {'email':.., 'name':..}} for TLs missing from HC."""
    aliases = {k.lower(): v for k, v in (aliases or {}).items()}
    _, hc_rows = read_dicts(path, hc_sheet, 1)
    _, st_rows = read_dicts(path, structure_sheet, 1)

    ref = ReferenceData()

    # HC master keyed case-insensitively; keep HC's canonical casing + row.
    hc_by_key = {}
    for row in hc_rows:
        crm = _clean(pick(row, "crm"))
        if crm is None:
            continue
        if _is_junk_crm(crm):
            ref.exceptions.append(RefException(crm, "invalid_crm", "malformed CRM in HC"))
            continue
        hc_by_key.setdefault(_key(crm), (crm, row))

    st_map = _structure_map(st_rows)

    # Managers = distinct Team Leaders; resolve via HC, then alias, else flag.
    tl_by_key = {}
    for m in st_map.values():
        if m["tl_key"]:
            tl_by_key.setdefault(m["tl_key"], m["tl_raw"])

    managers = {}
    for tl_key, tl_raw in sorted(tl_by_key.items()):
        if tl_key in hc_by_key:
            canon, row = hc_by_key[tl_key]
            mgr = Manager(crm=canon,
                          name=_clean(pick(row, "full name", "name")),
                          email=_clean(pick(row, "work email", "email")))
            if not mgr.email and tl_key in aliases:
                mgr.email = aliases[tl_key].get("email")
                mgr.name = mgr.name or aliases[tl_key].get("name")
            if not mgr.email:
                ref.exceptions.append(RefException(canon, "manager_no_email"))
        elif tl_key in aliases:
            mgr = Manager(crm=tl_raw,
                          name=aliases[tl_key].get("name"),
                          email=aliases[tl_key].get("email"))
        else:
            mgr = Manager(crm=tl_raw)
            ref.exceptions.append(RefException(tl_raw, "manager_not_in_hc"))
        managers[tl_key] = mgr
    ref.managers = list(managers.values())

    # Employees from HC (excluding Departed), joined to their TL via Structure.
    for key, (canon, row) in hc_by_key.items():
        status = _clean(pick(row, "employee status"))
        if status and status.lower() == "departed":
            continue
        tl_crm = sm = team = None
        if key in st_map:
            m = st_map[key]
            sm, team = m["sm_raw"], m["team"]
            if m["tl_key"] is None:
                ref.exceptions.append(RefException(canon, "unmapped_employee", "no TL in Structure"))
            else:
                mgr = managers.get(m["tl_key"])
                tl_crm = mgr.crm if mgr else None
        else:
            ref.exceptions.append(RefException(canon, "unmapped_employee", "no Structure row"))
        ref.employees.append(Employee(
            crm=canon,
            ps_id=_clean(pick(row, "ps id", "ps")),
            name=_clean(pick(row, "full name", "name")),
            email=_clean(pick(row, "work email", "email")),
            department=_clean(pick(row, "department")),
            vendor=_clean(pick(row, "vendor")),
            employee_status=status,
            join_date=to_date(pick(row, "join date")),
            exit_date=to_date(pick(row, "exit date")),
            manager_crm=tl_crm, sm_crm=sm, team=team,
        ))

    # People who appear in Structure but have no HC record.
    for key, m in st_map.items():
        if key not in hc_by_key:
            ref.exceptions.append(RefException(m["emp_raw"], "employee_not_in_hc", "in Structure, not in HC"))

    ref.stats = {
        "managers": len(ref.managers),
        "employees": len(ref.employees),
        "mapped_employees": sum(1 for e in ref.employees if e.manager_crm),
        "exceptions": len(ref.exceptions),
    }
    return ref
