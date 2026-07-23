import subprocess
from pathlib import Path

import pytest

from ds_tvbox.errors import PublishError
from ds_tvbox.gitops import Git, validate_ref, validate_sha


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - test helper uses an argv list only
        ["/usr/bin/git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    _git(remote, "init", "--bare")
    _git(local, "init", "-b", "main")
    _git(local, "config", "user.name", "test")
    _git(local, "config", "user.email", "test@example.invalid")
    _git(local, "remote", "add", "origin", str(remote))
    (local / "README.md").write_text("main\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(local, "commit", "-m", "init")
    _git(local, "push", "-u", "origin", "main")
    return local, remote, _git(local, "rev-parse", "HEAD")


def _commit(local: Path, branch: str, base: str, name: str) -> str:
    _git(local, "checkout", "-B", branch, base)
    (local / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(local, "add", f"{name}.txt")
    _git(local, "commit", "-m", name)
    return _git(local, "rev-parse", "HEAD")


def test_managed_ref_and_sha_validation() -> None:
    assert validate_ref("generated") == "generated"
    assert validate_ref("candidate/run-123-attempt-2").startswith("candidate/")
    assert validate_sha("a" * 40) == "a" * 40
    with pytest.raises(PublishError):
        validate_ref("main")
    with pytest.raises(PublishError):
        validate_sha("HEAD")


def test_git_fast_forward_and_precise_bootstrap_delete(tmp_path: Path) -> None:
    local, _remote, sha = _repository(tmp_path)
    git = Git(local)
    assert git.remote_head() is None
    git.push_if_remote_equals(sha, "generated", None)
    assert git.remote_head() == sha
    git.delete_bootstrap_with_lease(sha)
    assert git.remote_head() is None


def test_git_creates_unreferenced_empty_child_for_preflight(tmp_path: Path) -> None:
    local, _remote, parent = _repository(tmp_path)
    git = Git(local)

    child = git.create_empty_child(parent, "chore: 权限预检")

    assert _git(local, "rev-parse", f"{child}^") == parent
    assert _git(local, "rev-parse", f"{child}^{{tree}}") == _git(
        local, "rev-parse", f"{parent}^{{tree}}"
    )
    assert _git(local, "show", "-s", "--format=%s", child) == "chore: 权限预检"
    with pytest.raises(PublishError, match="commit message"):
        git.create_empty_child(parent, "bad\nmessage")


def test_git_expected_oid_cas_allows_only_fast_forward_and_exact_candidate_delete(
    tmp_path: Path,
) -> None:
    local, _remote, first = _repository(tmp_path)
    git = Git(local)
    git.push_if_remote_equals(first, "generated", None)
    second = _commit(local, "second", first, "second")
    git.push_if_remote_equals(second, "generated", first)
    assert git.remote_head() == second

    candidate = "candidate/run-123-attempt-1"
    git.push_if_remote_equals(second, candidate, None)
    assert git.remote_head(candidate) == second
    git.delete_candidate(candidate, second)
    assert git.remote_head(candidate) is None

    third = _commit(local, "third", second, "third")
    with pytest.raises(PublishError, match="remote generated changed"):
        git.push_if_remote_equals(third, "generated", first)
    assert git.remote_head() == second


def test_git_expected_oid_cas_rejects_non_fast_forward_commit(tmp_path: Path) -> None:
    local, _remote, first = _repository(tmp_path)
    git = Git(local)
    git.push_if_remote_equals(first, "generated", None)
    second = _commit(local, "second", first, "second")
    git.push_if_remote_equals(second, "generated", first)
    divergent = _commit(local, "divergent", first, "divergent")

    with pytest.raises(PublishError, match="non-fast-forward CAS"):
        git.push_if_remote_equals(divergent, "generated", second)
    assert git.remote_head() == second


def test_git_expected_oid_lease_closes_race_between_precheck_and_push(
    tmp_path: Path,
) -> None:
    local, _remote, first = _repository(tmp_path)
    ordinary = Git(local)
    ordinary.push_if_remote_equals(first, "generated", None)
    candidate = _commit(local, "candidate", first, "candidate")
    race = _commit(local, "race", first, "race")

    class RaceGit(Git):
        raced = False

        def run(self, *arguments: str, **kwargs: object):  # type: ignore[no-untyped-def]
            if (
                not self.raced
                and arguments
                and arguments[0] == "push"
                and any(value.startswith("--force-with-lease=") for value in arguments)
            ):
                self.raced = True
                _git(local, "push", "origin", f"{race}:refs/heads/generated")
            return super().run(*arguments, **kwargs)  # type: ignore[arg-type]

    git = RaceGit(local)
    with pytest.raises(PublishError, match="stale info"):
        git.push_if_remote_equals(candidate, "generated", first)
    assert git.remote_head() == race


def test_git_exact_candidate_delete_rejects_a_raced_ref(tmp_path: Path) -> None:
    local, _remote, first = _repository(tmp_path)
    candidate_ref = "candidate/run-124-attempt-1"
    ordinary = Git(local)
    ordinary.push_if_remote_equals(first, candidate_ref, None)
    race = _commit(local, "race", first, "race")

    class RaceDeleteGit(Git):
        raced = False

        def run(self, *arguments: str, **kwargs: object):  # type: ignore[no-untyped-def]
            if (
                not self.raced
                and arguments
                and arguments[0] == "push"
                and arguments[-1] == f":refs/heads/{candidate_ref}"
            ):
                self.raced = True
                _git(local, "push", "origin", f"{race}:refs/heads/{candidate_ref}")
            return super().run(*arguments, **kwargs)  # type: ignore[arg-type]

    git = RaceDeleteGit(local)
    with pytest.raises(PublishError, match="stale info"):
        git.delete_candidate(candidate_ref, first)
    assert git.remote_head(candidate_ref) == race
