"""The Export tab's download gate, kept out of Streamlit so it can be unit-tested.

A built reconciled report is held in session state with the (file hash, year) it was built from.
The HRBP must tick the acknowledgement before downloading whenever the report carries unresolved
(Pending) or not-in-workbook cases; blank cells are information only. Every new build, and any
change of file or year, drops both the report and the tick, so an old tick never covers a new
report.
"""
BUILT_KEY = "reconciled"
ACK_KEY = "reconcile_ack"


def needs_ack(result) -> bool:
    """Whether this report's download waits for the acknowledgement tick."""
    return bool(result.unresolved or result.not_in_workbook)


def download_enabled(result, ticked) -> bool:
    return (not needs_ack(result)) or bool(ticked)


def reset(state) -> None:
    """Forget the built report and its tick (before a rebuild)."""
    state.pop(BUILT_KEY, None)
    state.pop(ACK_KEY, None)


def current(state, key):
    """The built report for this (file hash, year) key, or None; a stale one is reset."""
    built = state.get(BUILT_KEY)
    if built and built["key"] != key:
        reset(state)
        return None
    return built
