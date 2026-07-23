from __future__ import annotations

import json
import shutil
import subprocess
from argparse import ArgumentTypeError
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ds_tvbox.cli import (
    _bool_arg,
    _compatibility,
    _decision_payload,
    _decode_object,
    _exact_remote_state,
    _load_mapping,
    _policy,
    _raw_verifier,
    _validate_denylist_matchers,
    _write_github_output,
    main,
)
from ds_tvbox.errors import ContractError, PublishError
from ds_tvbox.schedule import DueDecision


def test_boolean_argument_is_explicit() -> None:
    assert _bool_arg("true") is True
    assert _bool_arg("OFF") is False
    with pytest.raises(ArgumentTypeError):
        _bool_arg("perhaps")


def test_decision_payload_uses_lowercase_booleans() -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    decision = DueDecision(
        should_refresh=True,
        due=True,
        forced=False,
        recovery_due=False,
        bootstrap_required=False,
        reason="elapsed_240h",
        last_success_at=now,
        next_due_at=now,
    )
    payload = _decision_payload(decision, "a" * 40)
    assert payload["should_refresh"] == "true"
    assert payload["forced"] == "false"
    assert payload["previous_head"] == "a" * 40


def test_validate_static_accepts_repository_contract(capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[2]
    assert main(["validate-static", "--repository", str(root)]) == 0
    assert "static validation passed" in capsys.readouterr().out


def test_validate_static_rejects_malformed_schema_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = Path(__file__).resolve().parents[2]
    for relative in ("config", "schemas", "sources"):
        shutil.copytree(project / relative, tmp_path / relative)
    (tmp_path / "schemas/report.schema.json").write_text("{", encoding="utf-8")
    assert main(["validate-static", "--repository", str(tmp_path)]) == 2
    assert "invalid schema JSON" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/path",
        "https://example.com/path?to%5Fken=secret",
        "https://example.com/path?safe=1&AUTH=x&AUTH=y",
        "https://127.0.0.1/path",
        "https://Example.com/path",
        "https://example.com/path#fragment",
    ],
)
def test_denylist_matcher_urls_reject_credentials_and_noncanonical_forms(url: str) -> None:
    with pytest.raises(ContractError):
        _validate_denylist_matchers(
            {
                "entries": [
                    {
                        "id": "blocked",
                        "hosts": [],
                        "urls": [url],
                    }
                ]
            }
        )


def test_denylist_matchers_require_canonical_dns_hosts_and_https_urls() -> None:
    _validate_denylist_matchers(
        {
            "entries": [
                {
                    "id": "blocked",
                    "hosts": ["media.example.com"],
                    "urls": ["https://media.example.com/public.m3u8?safe=1"],
                }
            ]
        }
    )
    with pytest.raises(ContractError):
        _validate_denylist_matchers(
            {"entries": [{"id": "blocked", "hosts": ["Media.Example.com"], "urls": []}]}
        )


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable and test-owned argv
        ["/usr/bin/git", *arguments],
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def test_due_check_reports_bootstrap_without_creating_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    _git(tmp_path, "init", "--bare", str(remote))
    local.mkdir()
    _git(local, "init", "-b", "main")
    _git(local, "remote", "add", "origin", str(remote))
    output = tmp_path / "github-output"
    assert (
        main(
            [
                "due-check",
                "--repository",
                str(local),
                "--force",
                "false",
                "--bootstrap",
                "false",
                "--github-output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["bootstrap_required"] == "true"
    assert payload["should_refresh"] == "false"
    assert "bootstrap_required=true\n" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "data",
    ["{", "[]", '{"a":1,"a":2}'],
)
def test_cli_json_decoder_rejects_invalid_nonobject_and_duplicate_input(data: str) -> None:
    with pytest.raises(ContractError):
        _decode_object(data, "fixture")


def test_cli_mapping_loader_reports_missing_and_nonmapping_yaml(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="cannot read"):
        _load_mapping(tmp_path / "missing.yaml")
    path = tmp_path / "list.yaml"
    path.write_text("- one\n", encoding="utf-8")
    with pytest.raises(ContractError, match="must contain a mapping"):
        _load_mapping(path)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({}, "fields are invalid"),
        (
            {
                "version": 2,
                "owner": "owner",
                "repository": "repo",
                "generated_ref": "generated",
                "clients": ["tvbox"],
            },
            "version/ref is unsupported",
        ),
        (
            {
                "version": 1,
                "owner": "bad/owner",
                "repository": "repo",
                "generated_ref": "generated",
                "clients": ["tvbox"],
            },
            "owner is invalid",
        ),
        (
            {
                "version": 1,
                "owner": "owner",
                "repository": "bad/repo",
                "generated_ref": "generated",
                "clients": ["tvbox"],
            },
            "repository is invalid",
        ),
        (
            {
                "version": 1,
                "owner": "owner",
                "repository": "repo",
                "generated_ref": "generated",
                "clients": [],
            },
            "baseline is empty",
        ),
    ],
)
def test_compatibility_contract_rejects_ambiguous_values(
    tmp_path: Path,
    value: dict[str, object],
    error: str,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "compatibility.yaml").write_text(
        yaml.safe_dump(value), encoding="utf-8"
    )
    with pytest.raises(ContractError, match=error):
        _compatibility(tmp_path)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"version": 2}, "version is unsupported"),
        ({"version": 1, "refresh": []}, "must be exactly 240 hours"),
        ({"version": 1, "refresh": {"due_hours": 239}}, "must be exactly 240 hours"),
    ],
)
def test_policy_contract_requires_exact_refresh_interval(
    tmp_path: Path,
    value: dict[str, object],
    error: str,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "policy.yaml").write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ContractError, match=error):
        _policy(tmp_path)


def test_exact_remote_state_handles_absent_invalid_and_valid_state() -> None:
    class FakeGit:
        def __init__(self, result: SimpleNamespace) -> None:
            self.result = result
            self.fetched: list[str] = []

        def fetch_sha(self, sha: str) -> None:
            self.fetched.append(sha)

        def run(self, *_args: str, **_kwargs: object) -> SimpleNamespace:
            return self.result

    assert _exact_remote_state(FakeGit(SimpleNamespace()), None) is None  # type: ignore[arg-type]
    failed = FakeGit(SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(ContractError, match="without state"):
        _exact_remote_state(failed, "a" * 40)  # type: ignore[arg-type]
    valid = FakeGit(SimpleNamespace(returncode=0, stdout='{"status":"success"}'))
    assert _exact_remote_state(valid, "b" * 40) == {"status": "success"}  # type: ignore[arg-type]
    assert valid.fetched == ["b" * 40]


def test_github_output_is_optional_append_only_and_newline_safe(tmp_path: Path) -> None:
    _write_github_output(None, {"key": "value"})
    output = tmp_path / "nested/output"
    _write_github_output(output, {"one": "1"})
    _write_github_output(output, {"two": "2"})
    assert output.read_text(encoding="utf-8") == "one=1\ntwo=2\n"
    with pytest.raises(ContractError, match="contains a newline"):
        _write_github_output(output, {"unsafe": "a\nb"})


def test_raw_verifier_uses_compatibility_identity(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "schemas").mkdir()
    (config / "compatibility.yaml").write_text(
        """version: 1
owner: owner
repository: repository
generated_ref: generated
clients:
  - tvbox
""",
        encoding="utf-8",
    )
    verifier = _raw_verifier(tmp_path)
    assert verifier.owner == "owner"
    assert verifier.repository == "repository"


def test_cli_verify_artifact_publish_verify_release_and_collect_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = SimpleNamespace(
        generation=7,
        release_id="g00000007",
        release_kind=SimpleNamespace(value="regular"),
    )
    monkeypatch.setattr("ds_tvbox.cli.validate_publish_artifact", lambda *_args: artifact)
    assert main(["verify-artifact", "--repository", str(tmp_path), str(tmp_path / "a")]) == 0
    assert json.loads(capsys.readouterr().out)["release_id"] == "g00000007"

    class FakePublisher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def publish(self, path: Path) -> str:
            assert path == tmp_path / "a"
            return "a" * 40

    monkeypatch.setattr("ds_tvbox.cli.Publisher", FakePublisher)
    monkeypatch.setattr("ds_tvbox.cli._raw_verifier", lambda _root: object())
    assert (
        main(
            [
                "publish",
                "--repository",
                str(tmp_path),
                "--artifact",
                str(tmp_path / "a"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["published_sha"] == "a" * 40

    calls: list[tuple[str, int, int, str]] = []

    class FakeRaw:
        def poll_revision(
            self,
            revision: str,
            *,
            timeout_seconds: int,
            interval_seconds: int,
            expected_status: str,
        ) -> None:
            calls.append((revision, timeout_seconds, interval_seconds, expected_status))

    monkeypatch.setattr("ds_tvbox.cli._raw_verifier", lambda _root: FakeRaw())
    assert (
        main(
            [
                "verify-release",
                "--repository",
                str(tmp_path),
                "b" * 40,
                "--expected-status",
                "pending",
                "--timeout",
                "3",
                "--interval",
                "1",
            ]
        )
        == 0
    )
    assert calls == [("b" * 40, 3, 1, "pending")]
    capsys.readouterr()

    collected: list[dict[str, object]] = []

    def collect(**kwargs: object) -> None:
        collected.append(kwargs)

    monkeypatch.setattr("ds_tvbox.pipeline.collect_publish_artifact", collect)
    output = tmp_path / "output"
    assert (
        main(
            [
                "collect",
                "--repository",
                str(tmp_path),
                "--output",
                str(output),
                "--force",
                "true",
                "--bootstrap",
                "false",
            ]
        )
        == 0
    )
    assert collected == [
        {
            "repository": tmp_path.resolve(),
            "output": output.resolve(),
            "force": True,
            "bootstrap": False,
        }
    ]


def test_cli_main_maps_domain_and_os_errors_to_exit_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "ds_tvbox.cli.validate_publish_artifact",
        lambda *_args: (_ for _ in ()).throw(PublishError("blocked")),
    )
    assert main(["verify-artifact", "--repository", str(tmp_path), str(tmp_path)]) == 2
    assert "error: blocked" in capsys.readouterr().err


def test_preflight_ref_verifies_fast_forward_ruleset_and_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeGit:
        remote = "origin"
        base_sha = "a" * 40
        fast_forward_sha = "b" * 40
        divergent_sha = "c" * 40

        def __init__(self, _root: Path) -> None:
            self.current: str | None = None
            self.pushed: list[tuple[str, str, str | None]] = []
            self.children = iter((self.fast_forward_sha, self.divergent_sha))
            self.deleted: tuple[str, str] | None = None

        def remote_head(self, _ref: str) -> str | None:
            return self.current

        def run(self, *args: str, **_kwargs: object) -> SimpleNamespace:
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(
                    stdout=self.base_sha + "\n", stderr="", returncode=0
                )
            assert args[0] == "push"
            return SimpleNamespace(
                stdout="",
                stderr="remote: error: GH013: Cannot force-push to this branch",
                returncode=1,
            )

        def create_empty_child(self, parent: str, _message: str) -> str:
            assert parent == self.base_sha
            return next(self.children)

        def push_if_remote_equals(
            self, sha: str, ref: str, expected_head: str | None
        ) -> None:
            assert self.current == expected_head
            self.pushed.append((sha, ref, expected_head))
            self.current = sha

        def delete_candidate(self, ref: str, expected_sha: str) -> None:
            assert self.current == expected_sha
            self.deleted = (ref, expected_sha)
            self.current = None

    fake = FakeGit(tmp_path)
    monkeypatch.setattr("ds_tvbox.cli.Git", lambda _root: fake)
    monkeypatch.setenv("GITHUB_RUN_ID", "55")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    assert main(["preflight-ref", "--repository", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == "candidate/run-preflight-55-attempt-3"
    assert fake.pushed == [
        (fake.base_sha, payload["ref"], None),
        (fake.fast_forward_sha, payload["ref"], fake.base_sha),
    ]
    assert fake.deleted == (payload["ref"], fake.fast_forward_sha)

    class Existing(FakeGit):
        def __init__(self, _root: Path) -> None:
            super().__init__(_root)
            self.current = self.fast_forward_sha
            self.pushed = []

    monkeypatch.setattr("ds_tvbox.cli.Git", Existing)
    assert main(["preflight-ref", "--repository", str(tmp_path)]) == 2
    assert "already exists" in capsys.readouterr().err

    class ForceAllowed(FakeGit):
        def run(self, *args: str, **kwargs: object) -> SimpleNamespace:
            if args[:2] == ("rev-parse", "HEAD"):
                return super().run(*args, **kwargs)
            self.current = self.divergent_sha
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    allowed = ForceAllowed(tmp_path)
    monkeypatch.setattr("ds_tvbox.cli.Git", lambda _root: allowed)
    assert main(["preflight-ref", "--repository", str(tmp_path)]) == 2
    assert "allowed a non-fast-forward force push" in capsys.readouterr().err
    assert allowed.current is None

    class AmbiguousFailure(FakeGit):
        def run(self, *args: str, **kwargs: object) -> SimpleNamespace:
            if args[:2] == ("rev-parse", "HEAD"):
                return super().run(*args, **kwargs)
            return SimpleNamespace(stdout="", stderr="network reset", returncode=1)

    ambiguous = AmbiguousFailure(tmp_path)
    monkeypatch.setattr("ds_tvbox.cli.Git", lambda _root: ambiguous)
    assert main(["preflight-ref", "--repository", str(tmp_path)]) == 2
    assert "explicit GitHub ruleset rejection" in capsys.readouterr().err
    assert ambiguous.current == ambiguous.fast_forward_sha

    class ForceAllowedRace(ForceAllowed):
        raced_sha = "d" * 40

        def run(self, *args: str, **kwargs: object) -> SimpleNamespace:
            result = super().run(*args, **kwargs)
            if args[:2] != ("rev-parse", "HEAD"):
                self.current = self.raced_sha
            return result

    allowed_race = ForceAllowedRace(tmp_path)
    monkeypatch.setattr("ds_tvbox.cli.Git", lambda _root: allowed_race)
    assert main(["preflight-ref", "--repository", str(tmp_path)]) == 2
    assert "allowed a non-fast-forward force push" in capsys.readouterr().err
    assert allowed_race.current == allowed_race.raced_sha
    assert allowed_race.deleted is None

    class RejectedRace(FakeGit):
        raced_sha = "e" * 40

        def run(self, *args: str, **kwargs: object) -> SimpleNamespace:
            result = super().run(*args, **kwargs)
            if args[:2] != ("rev-parse", "HEAD"):
                self.current = self.raced_sha
            return result

    rejected_race = RejectedRace(tmp_path)
    monkeypatch.setattr("ds_tvbox.cli.Git", lambda _root: rejected_race)
    assert main(["preflight-ref", "--repository", str(tmp_path)]) == 2
    assert "changed the remote ref" in capsys.readouterr().err
    assert rejected_race.current == rejected_race.raced_sha
    assert rejected_race.deleted is None
