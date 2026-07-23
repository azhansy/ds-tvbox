"""Safe, argv-only Git operations for the publisher transaction."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ds_tvbox.errors import PublishError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^(?:generated|candidate/run-[0-9A-Za-z_.-]+-attempt-[1-9][0-9]*)$")


def validate_sha(value: str) -> str:
    if not _SHA.fullmatch(value):
        raise PublishError(f"invalid Git SHA: {value!r}")
    return value


def validate_ref(value: str) -> str:
    if not _REF.fullmatch(value):
        raise PublishError(f"invalid managed Git ref: {value!r}")
    return value


class Git:
    def __init__(self, repository: Path, remote: str = "origin") -> None:
        self.repository = repository.resolve()
        self.remote = remote

    def run(
        self,
        *arguments: str,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", *arguments]
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        result = subprocess.run(  # noqa: S603 - argv is validated and shell is never used
            command,
            cwd=(cwd or self.repository),
            env=process_env,
            check=False,
            text=True,
            capture_output=True,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise PublishError(f"git {' '.join(arguments[:2])} failed: {message}")
        return result

    def remote_head(self, ref: str = "generated") -> str | None:
        validate_ref(ref)
        result = self.run(
            "ls-remote",
            "--heads",
            self.remote,
            f"refs/heads/{ref}",
        )
        output = result.stdout.strip()
        if not output:
            return None
        fields = output.split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{ref}":
            raise PublishError(f"unexpected ls-remote result for {ref}")
        return validate_sha(fields[0])

    def fetch_sha(self, sha: str) -> None:
        self.run("fetch", "--no-tags", self.remote, validate_sha(sha))

    def push_new_or_fast_forward(self, sha: str, ref: str) -> None:
        self.run(
            "push",
            self.remote,
            f"{validate_sha(sha)}:refs/heads/{validate_ref(ref)}",
        )

    def create_empty_child(self, parent: str, message: str) -> str:
        """Create an unreferenced empty child commit for permission preflight."""

        parent = validate_sha(parent)
        if not message or "\n" in message:
            raise PublishError("commit message must be a single non-empty line")
        tree = validate_sha(
            self.run("rev-parse", f"{parent}^{{tree}}").stdout.strip()
        )
        result = self.run(
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            message,
        )
        return validate_sha(result.stdout.strip())

    def push_if_remote_equals(
        self,
        sha: str,
        ref: str,
        expected_head: str | None,
    ) -> None:
        """Perform an expected-OID CAS while preserving fast-forward-only semantics."""

        sha = validate_sha(sha)
        ref = validate_ref(ref)
        expected = validate_sha(expected_head) if expected_head is not None else None
        actual = self.remote_head(ref)
        if actual != expected:
            raise PublishError(
                f"remote {ref} changed: expected {expected or '<absent>'}, "
                f"found {actual or '<absent>'}"
            )
        if expected is not None:
            self.fetch_sha(expected)
            ancestry = self.run(
                "merge-base",
                "--is-ancestor",
                expected,
                sha,
                check=False,
            )
            if ancestry.returncode != 0:
                raise PublishError(f"refusing non-fast-forward CAS update for {ref}")
        lease = f"--force-with-lease=refs/heads/{ref}:{expected or ''}"
        self.run(
            "push",
            lease,
            self.remote,
            f"{sha}:refs/heads/{ref}",
        )
        if self.remote_head(ref) != sha:
            raise PublishError(f"remote {ref} does not equal the published commit")

    def delete_ref_with_lease(self, ref: str, expected_sha: str) -> None:
        ref = validate_ref(ref)
        sha = validate_sha(expected_sha)
        if self.remote_head(ref) != sha:
            raise PublishError(f"remote {ref} changed before precise deletion")
        self.run(
            "push",
            f"--force-with-lease=refs/heads/{ref}:{sha}",
            self.remote,
            f":refs/heads/{ref}",
        )
        if self.remote_head(ref) is not None:
            raise PublishError(f"remote {ref} precise deletion was not observable")

    def delete_candidate(self, ref: str, expected_sha: str) -> None:
        ref = validate_ref(ref)
        if not ref.startswith("candidate/"):
            raise PublishError("only candidate refs can be normally deleted")
        self.delete_ref_with_lease(ref, expected_sha)

    def delete_bootstrap_with_lease(self, expected_sha: str) -> None:
        self.delete_ref_with_lease("generated", expected_sha)

    @contextmanager
    def worktree(self, base_sha: str | None, *, orphan: bool = False) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="ds-tvbox-publish-") as temporary:
            path = Path(temporary).resolve()
            orphan_branch: str | None = None
            if base_sha is None:
                base = self.run("rev-parse", "HEAD").stdout.strip()
                validate_sha(base)
            else:
                base = validate_sha(base_sha)
                self.fetch_sha(base)
            self.run("worktree", "add", "--detach", str(path), base)
            try:
                if orphan:
                    orphan_branch = f"ds-tvbox-bootstrap-{path.name}"
                    self.run("checkout", "--orphan", orphan_branch, cwd=path)
                    self.run("read-tree", "--empty", cwd=path)
                    for child in list(path.iterdir()):
                        if child.name == ".git":
                            continue
                        if child.is_dir() and not child.is_symlink():
                            import shutil

                            shutil.rmtree(child)
                        else:
                            child.unlink()
                yield path
            finally:
                self.run("worktree", "remove", "--force", str(path), check=False)
                if orphan_branch is not None:
                    self.run("branch", "-D", orphan_branch, check=False)

    def commit(self, cwd: Path, message: str, paths: Sequence[str]) -> str:
        if not message or "\n" in message:
            raise PublishError("commit message must be a single non-empty line")
        self.run("add", "--all", "--", *paths, cwd=cwd)
        commit_env = {}
        if os.environ.get("GIT_AUTHOR_DATE"):
            commit_env["GIT_AUTHOR_DATE"] = os.environ["GIT_AUTHOR_DATE"]
        self.run(
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            message,
            cwd=cwd,
            env=commit_env,
        )
        sha = self.run("rev-parse", "HEAD", cwd=cwd).stdout.strip()
        return validate_sha(sha)

    def changed_paths(self, older: str, newer: str, cwd: Path | None = None) -> tuple[str, ...]:
        result = self.run(
            "diff",
            "--name-only",
            validate_sha(older),
            validate_sha(newer),
            cwd=cwd,
        )
        return tuple(line for line in result.stdout.splitlines() if line)
