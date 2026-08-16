"""Locks the deploy-provenance stamp: which commit the running app was built from.

Streamlit Community Cloud serves whatever it last cloned, and a code-only push can leave a warm
container running stale code with no visible difference. These tests pin the .git reading so the
stamp stays trustworthy — and degrades to None rather than raising when .git is absent."""
import pytest

from app.version import build_stamp, commit_sha, rev_marker

SHA = "d49a874f1c2b3a49585e6d7c8b9a0f1e2d3c4b5a"


def _git(root, head, refs=None, packed=None):
    """Build a minimal .git under `root`. refs: {ref_name: sha}. packed: raw packed-refs text."""
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text(head, encoding="utf-8")
    for name, sha in (refs or {}).items():
        p = git / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha, encoding="utf-8")
    if packed is not None:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return root


def test_reads_sha_from_a_loose_ref(tmp_path):
    _git(tmp_path, "ref: refs/heads/master\n", {"refs/heads/master": SHA + "\n"})
    assert commit_sha(tmp_path) == SHA[:7]


def test_full_sha_when_short_is_false(tmp_path):
    _git(tmp_path, "ref: refs/heads/master\n", {"refs/heads/master": SHA + "\n"})
    assert commit_sha(tmp_path, short=False) == SHA


def test_falls_back_to_packed_refs_when_the_loose_ref_is_absent(tmp_path):
    # A freshly cloned repo commonly has its refs packed rather than written out individually.
    _git(tmp_path, "ref: refs/heads/master\n",
         packed=f"# pack-refs with: peeled fully-peeled sorted\n{SHA} refs/heads/master\n")
    assert commit_sha(tmp_path) == SHA[:7]


def test_packed_refs_ignores_peel_lines(tmp_path):
    # '^' lines carry the peeled tag target and must never be mistaken for the ref's own sha.
    packed = (f"# pack-refs with: peeled\n"
              f"1111111111111111111111111111111111111111 refs/tags/v1\n"
              f"^2222222222222222222222222222222222222222\n"
              f"{SHA} refs/heads/master\n")
    _git(tmp_path, "ref: refs/heads/master\n", packed=packed)
    assert commit_sha(tmp_path) == SHA[:7]


def test_detached_head_holds_the_sha_directly(tmp_path):
    _git(tmp_path, SHA + "\n")
    assert commit_sha(tmp_path) == SHA[:7]


def test_returns_none_without_a_git_directory(tmp_path):
    assert commit_sha(tmp_path) is None


def test_returns_none_when_the_ref_cannot_be_resolved(tmp_path):
    _git(tmp_path, "ref: refs/heads/master\n")      # no loose ref, no packed-refs
    assert commit_sha(tmp_path) is None


def test_rev_marker_takes_the_last_marker(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "openpyxl==3.1.5\n# rev: first marker\nstreamlit==1.59.0\n# rev: second marker\n",
        encoding="utf-8")
    assert rev_marker(tmp_path) == "second marker"


@pytest.mark.parametrize("content", ["openpyxl==3.1.5\n", ""])
def test_rev_marker_is_none_when_absent(tmp_path, content):
    (tmp_path / "requirements.txt").write_text(content, encoding="utf-8")
    assert rev_marker(tmp_path) is None


def test_rev_marker_is_none_without_requirements(tmp_path):
    assert rev_marker(tmp_path) is None


def test_build_stamp_prefers_the_commit(tmp_path):
    _git(tmp_path, "ref: refs/heads/master\n", {"refs/heads/master": SHA + "\n"})
    (tmp_path / "requirements.txt").write_text("# rev: carve-out\n", encoding="utf-8")
    assert build_stamp(tmp_path) == f"build {SHA[:7]}"


def test_build_stamp_falls_back_to_the_rev_marker(tmp_path):
    # Deployments that ship a source archive have no .git; the hand-bumped marker is all we have.
    (tmp_path / "requirements.txt").write_text("# rev: carve-out\n", encoding="utf-8")
    assert build_stamp(tmp_path) == "build carve-out"


def test_build_stamp_reports_unknown_when_nothing_is_available(tmp_path):
    assert build_stamp(tmp_path) == "build unknown"
