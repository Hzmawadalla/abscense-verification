"""Single source of truth for classifying an attendance day-cell status (SPEC §4).

Used by both the seed generator (tools/gen_status_vocabulary.py) and the runtime Summary Report
parser, so the vocabulary in the database and the live parser can never disagree.

classify() returns (bucket, canonical_status, is_half_day) where bucket is one of:
  skip | not_verified | trigger | ignore | unknown
'unknown' is not a stored vocab bucket — callers decide (the generator files it as 'ignore' for
review; ingestion raises an exception so a human maps it)."""
import re

SKIP_CLEAN = {
    "normal", "weekend", "public holiday", "not yet hired",
    "present", "no leave", "leave approved", "leave approval",
}
SKIP_LEAVES = {
    "annual leave", "sick leave", "casual leave", "continuing education leave",
    "paternity leave", "bereavement leave", "unpaid leave",
}
NOT_VERIFIED = {"late", "missing punch out", "half day", "halfday", "hlaf day", "2 hour excuse", "excuse"}
TRIGGER_EXACT = {"absent", "no show"}
KNOWN_NOISE = {"#n/a", "0", "departed", "active", "resigned"}
ANNOTATIONS = ("to be confirmed", "to be deducted", "deducted from balance",
               "pending", "failed", "returned", "applied on leave", "- check")

# Curated manual classifications for values that aren't algorithmically derivable
# (freeform notes / shorthand). Keyed on normalized raw value; checked first.
OVERRIDES = {
    "sick": ("skip", "Sick Leave"),
    "asked for leave, on trip": ("skip", "Leave"),
}


def normalize(raw) -> str:
    return re.sub(r"\s+", " ", str(raw).strip().lower())


def _base(n: str) -> str:
    b = re.sub(r"\(.*?\)", "", n)      # drop (HD), (Failed), (Returned) ...
    b = b.split(" - ")[0]             # drop " - Check" / " - To Be deducted ..."
    return re.sub(r"\s+", " ", b).strip()


def classify(raw):
    """Return (bucket, canonical_status, is_half_day)."""
    n = normalize(raw)
    if n == "":
        return ("skip", None, False)
    is_hd = "(hd)" in n or "half" in _base(n)
    if n in OVERRIDES:
        bucket, canon = OVERRIDES[n]
        return (bucket, canon, is_hd)
    b = _base(n)
    # Leave-balance bookkeeping is not a TL attendance dispute (balance is ignored, SPEC §4).
    if "deducted" in n or "from balance" in n:
        return ("skip", b.title() or None, is_hd)
    # Public holidays are org-wide, never a per-employee TL verification.
    if b == "public holiday":
        return ("skip", "Public Holiday", is_hd)
    # Explicit mid-dispute / failed annotations -> verify.
    if any(k in n for k in ANNOTATIONS):
        return ("trigger", b.title() or None, is_hd)
    if b in TRIGGER_EXACT:
        return ("trigger", b.title(), is_hd)
    if b in SKIP_CLEAN or b in SKIP_LEAVES:
        return ("skip", b.title(), is_hd)
    if b in NOT_VERIFIED:
        return ("not_verified", b.title(), is_hd)
    if n in KNOWN_NOISE or n.isdigit():
        return ("ignore", None, is_hd)
    return ("unknown", None, is_hd)
