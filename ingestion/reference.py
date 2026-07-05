"""Parse the HC + Structure tabs into managers, employees, and a mapping between them (SPEC §6.1).

Pure function over a workbook path — no database. Returns structured data the seed step upserts,
and an exceptions list for anything that can't be cleanly mapped (never silently dropped)."""
from dataclasses import dataclass, field

from .workbook import pick, read_dicts, to_date

PLACEHOLDERS = {"boot camp"}
_JUNK_CRM = {"n/a", "#n/a", "#ref!", "0", "na", "none"}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


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
    """employee_crm -> (tl_crm, sm_crm, team); plus the set of all CRMs seen in Structure."""
    mapping, seen = {}, set()
    for row in struct_rows:
        ecrm = _clean(pick(row, "crm"))
        if not ecrm or _is_junk_crm(ecrm):
            continue
        tl = _clean(pick(row, "tl/coach lead", "tl"))
        if tl and (tl.lower() in PLACEHOLDERS or _is_junk_crm(tl)):
            tl = None
        sm = _clean(pick(row, "sm/ltl/stl", "sm"))
        if sm and sm.lower() in PLACEHOLDERS:
            sm = None
        team = _clean(pick(row, "team"))
        mapping[ecrm] = (tl, sm, team)
        seen.add(ecrm)
    return mapping, seen


def parse_reference(path, hc_sheet="HC", structure_sheet="Structure"):
    _, hc_rows = read_dicts(path, hc_sheet, 1)
    _, st_rows = read_dicts(path, structure_sheet, 1)

    st_map, struct_crms = _structure_map(st_rows)

    ref = ReferenceData()

    hc_by_crm = {}
    for row in hc_rows:
        crm = _clean(pick(row, "crm"))
        if crm is None:
            continue
        if _is_junk_crm(crm):
            ref.exceptions.append(RefException(crm, "invalid_crm", "malformed CRM in HC"))
            continue
        if crm not in hc_by_crm:
            hc_by_crm[crm] = row

    # Managers = distinct Team Leaders, enriched from HC where possible.
    tl_crms = sorted({t[0] for t in st_map.values() if t[0]})
    managers = {}
    for tl in tl_crms:
        hc = hc_by_crm.get(tl)
        if hc:
            mgr = Manager(crm=tl,
                          name=_clean(pick(hc, "full name", "name")),
                          email=_clean(pick(hc, "work email", "email")))
            if not mgr.email:
                ref.exceptions.append(RefException(tl, "manager_no_email"))
        else:
            mgr = Manager(crm=tl)
            ref.exceptions.append(RefException(tl, "manager_not_in_hc"))
        managers[tl] = mgr
    ref.managers = list(managers.values())

    # Employees from HC (excluding Departed), joined to their TL via Structure.
    for crm, row in hc_by_crm.items():
        status = _clean(pick(row, "employee status"))
        if status and status.lower() == "departed":
            continue
        tl = sm = team = None
        if crm in st_map:
            tl, sm, team = st_map[crm]
            if tl is None:
                ref.exceptions.append(RefException(crm, "unmapped_employee", "no TL in Structure"))
        else:
            ref.exceptions.append(RefException(crm, "unmapped_employee", "no Structure row"))
        ref.employees.append(Employee(
            crm=crm,
            ps_id=_clean(pick(row, "ps id", "ps")),
            name=_clean(pick(row, "full name", "name")),
            email=_clean(pick(row, "work email", "email")),
            department=_clean(pick(row, "department")),
            vendor=_clean(pick(row, "vendor")),
            employee_status=status,
            join_date=to_date(pick(row, "join date")),
            exit_date=to_date(pick(row, "exit date")),
            manager_crm=tl, sm_crm=sm, team=team,
        ))

    # People who appear in Structure but have no HC record.
    for crm in sorted(struct_crms):
        if crm not in hc_by_crm:
            ref.exceptions.append(RefException(crm, "employee_not_in_hc", "in Structure, not in HC"))

    ref.stats = {
        "managers": len(ref.managers),
        "employees": len(ref.employees),
        "exceptions": len(ref.exceptions),
    }
    return ref
