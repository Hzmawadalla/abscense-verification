"""Deploy provenance — which commit the running app was built from.

Streamlit Community Cloud serves whatever it last cloned, and a code-only push can leave a warm
container running stale code. Nothing in the UI distinguished a current build from a stale one, so
an ingest run under superseded rules looked identical to a correct one. Surfacing the commit turns
'is production current?' into a glance: compare the stamp against `git rev-parse --short origin/master`.

HEAD is resolved by reading `.git` directly rather than shelling out to git — the binary is not
guaranteed on a Streamlit Cloud image, and a subprocess in the request path is a cost and a failure
mode this does not need. Everything degrades to None instead of raising: a missing stamp must never
be the reason the dashboard fails to render."""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_REV_RE = re.compile(r"^#\s*rev:\s*(.+?)\s*$", re.MULTILINE)


def _packed_ref(git: Path, name: str) -> str | None:
    """Resolve `name` from .git/packed-refs (a fresh clone often has no loose ref file)."""
    packed = git / "packed-refs"
    if not packed.is_file():
        return None
    for line in packed.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith("^"):   # comment / peeled-tag target
            continue
        sha, _, ref = line.partition(" ")
        if ref.strip() == name:
            return sha.strip()
    return None


def commit_sha(root=REPO_ROOT, short: bool = True) -> str | None:
    """The commit HEAD points at, or None when it can't be determined.

    Handles the three shapes HEAD takes: a symbolic ref to a loose ref file, a symbolic ref whose
    target is only in packed-refs, and a detached HEAD holding the sha itself."""
    git = Path(root) / ".git"
    head = git / "HEAD"
    if not head.is_file():
        return None
    contents = head.read_text(encoding="utf-8").strip()
    if contents.startswith("ref:"):
        name = contents.partition(":")[2].strip()
        loose = git / name
        sha = loose.read_text(encoding="utf-8").strip() if loose.is_file() else _packed_ref(git, name)
    else:
        sha = contents
    if not sha:
        return None
    return sha[:7] if short else sha


def rev_marker(root=REPO_ROOT) -> str | None:
    """The trailing `# rev: ...` note in requirements.txt.

    Bumping that line is how this repo forces Community Cloud to rebuild, so it doubles as a
    human-readable build label wherever .git isn't available. The last marker wins."""
    req = Path(root) / "requirements.txt"
    if not req.is_file():
        return None
    markers = _REV_RE.findall(req.read_text(encoding="utf-8"))
    return markers[-1].strip() if markers else None


def build_stamp(root=REPO_ROOT) -> str:
    """One-line provenance for the UI, e.g. 'build d49a874'. Never raises."""
    return f"build {commit_sha(root) or rev_marker(root) or 'unknown'}"
