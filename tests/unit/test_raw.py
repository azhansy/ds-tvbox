from __future__ import annotations

import io
import json
import time
import urllib.error
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

from ds_tvbox.bundle import build_bundle_files, materialize_bundle
from ds_tvbox.errors import ContractError, PublishError
from ds_tvbox.generator import build_client_artifacts
from ds_tvbox.manifests import prefixed_sha256
from ds_tvbox.models import ReleaseKind, RunContext
from ds_tvbox.raw import (
    RawExpectedRelease,
    RawResponse,
    RawVerifier,
    default_raw_fetch,
)
from ds_tvbox.reports import build_latest_report, render_latest_markdown
from ds_tvbox.serialization import canonical_json_bytes

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _release_tree(
    root: Path,
    *,
    generation: int = 1,
    run_id: str = "900",
    attempt: int = 2,
) -> None:
    kind = ReleaseKind.BOOTSTRAP if generation == 1 else ReleaseKind.REGULAR
    previous_head = None if generation == 1 else "b" * 40
    context = RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
        generated_at="2026-07-22T12:00:00Z",
        generation=generation,
        release_kind=kind,
        previous_head=previous_head,
        previous_last_success_at=(
            None if generation == 1 else "2026-07-12T12:00:00Z"
        ),
    )
    identity = {"workflow_run_id": run_id, "workflow_run_attempt": attempt}
    report = build_latest_report(
        context,
        status="success",
        started_at=context.generated_at,
        finished_at=context.generated_at,
        due=True,
        forced=True,
        recovery_due=False,
        sources=[],
        counts={},
        gate={"publish": True},
        previous_release_head_sha=previous_head,
        content_commit_sha="a" * 40,
        candidate_ref=context.candidate_ref,
        content_identity=identity,
    )
    state = {
        "schema_version": "1.0.0",
        "status": "success",
        "release_kind": kind.value,
        "generation": generation,
        "active_release_id": context.release_id,
        "last_publish_at": context.generated_at,
        "last_success_at": context.generated_at,
        "content_commit_sha": "a" * 40,
        "previous_release_head_sha": previous_head,
        "workflow_run_id": run_id,
        "workflow_run_attempt": attempt,
    }
    client = build_client_artifacts(
        context=context,
        sources=[],
        vod_results=[],
        channels=[],
    )
    bundle = build_bundle_files(
        context=context,
        client_artifacts=client,
        health={
            "schema_version": "1.0.0",
            "generated_at": context.generated_at,
            "generation": generation,
            "release_id": context.release_id,
            "sources": [],
            "channels": [],
        },
        source_count=0,
        supplemental_files={
            "state/release.json": canonical_json_bytes(state),
            "dist/reports/latest.json": canonical_json_bytes(report),
            "dist/reports/latest.md": render_latest_markdown(report),
        },
    )
    materialize_bundle(root, bundle)


def _raw_expectation(root: Path, *, event_generation: int | None = None) -> RawExpectedRelease:
    root_bytes = (root / "dist/manifest.json").read_bytes()
    manifest = json.loads(root_bytes)
    release_path = manifest["release_manifest"]["path"]
    release = json.loads((root / release_path).read_bytes())
    state = json.loads((root / "state/release.json").read_bytes())
    generation = int(release["generation"])
    return RawExpectedRelease(
        release_id=str(manifest["active_release_id"]),
        release_generation=generation,
        event_generation=int(state["generation"]) if event_generation is None else event_generation,
        workflow_run_id=str(state["workflow_run_id"]),
        workflow_run_attempt=int(state["workflow_run_attempt"]),
        content_workflow_run_id=str(manifest["content_workflow_run_id"]),
        content_workflow_run_attempt=int(manifest["content_workflow_run_attempt"]),
        root_manifest_sha256=prefixed_sha256(root_bytes),
        release_manifest_sha256=str(manifest["release_manifest"]["sha256"]),
        aliases={str(key): str(value) for key, value in manifest["aliases"].items()},
    )


def test_commit_and_bare_raw_download_the_complete_release_with_no_query(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    requested: list[str] = []

    def fetch(url: str, limit: int, timeout: float) -> RawResponse:
        assert timeout <= 15
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname == "raw.githubusercontent.com"
        assert not parts.query and not parts.fragment
        prefix = "/azhansy/ds-tvbox/"
        suffix = unquote(parts.path).removeprefix(prefix)
        _revision, relative = suffix.split("/", 1)
        requested.append(relative)
        body = (tmp_path / relative).read_bytes()
        assert len(body) <= limit
        return RawResponse(200, body)

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    expected = _raw_expectation(tmp_path)
    verifier.verify_revision("a" * 40, expected=expected)
    verifier.poll_bare(timeout_seconds=1, expected=expected)

    assert "dist/manifest.json" in requested
    assert "dist/releases/g00000001/manifest.json" in requested
    assert "dist/releases/g00000001/configs/stable.json" in requested
    assert "dist/index.json" in requested
    assert "state/release.json" in requested


def test_bare_raw_rejects_a_stale_but_internally_valid_release(tmp_path: Path) -> None:
    stale = tmp_path / "stale"
    current = tmp_path / "current"
    _release_tree(stale)
    _release_tree(current, generation=2, run_id="901", attempt=1)

    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        return RawResponse(200, (stale / relative).read_bytes())

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(PublishError, match="hash mismatch"):
        verifier.poll_bare(
            timeout_seconds=1,
            expected=_raw_expectation(current),
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"release_id": "g00000002"}, "active release"),
        (
            {"release_generation": 2, "event_generation": 2},
            "release generation",
        ),
        ({"event_generation": 2}, "state generation"),
        ({"workflow_run_id": "901"}, "state event"),
        ({"content_workflow_run_id": "901"}, "content identity"),
        ({"root_manifest_sha256": "sha256:" + "0" * 64}, "hash mismatch"),
        ({"release_manifest_sha256": "sha256:" + "0" * 64}, "release pointer"),
    ],
)
def test_raw_sealed_identity_rejects_each_mismatched_dimension(
    tmp_path: Path,
    mutation: Any,
    error: str,
) -> None:
    _release_tree(tmp_path)
    expected = replace(_raw_expectation(tmp_path), **mutation)

    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        return RawResponse(200, (tmp_path / relative).read_bytes())

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(ContractError, match=error):
        verifier.verify_revision("a" * 40, expected=expected)


def test_raw_sealed_identity_rejects_alias_mismatch(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    expected = _raw_expectation(tmp_path)
    aliases = dict(expected.aliases)
    aliases["dist/index.json"] = "sha256:" + "0" * 64
    mismatched = replace(expected, aliases=aliases)

    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        return RawResponse(200, (tmp_path / relative).read_bytes())

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(ContractError, match="aliases differ"):
        verifier.verify_revision("a" * 40, expected=mismatched)


def test_raw_sealed_identity_rejects_deletion_receipt_mismatch(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    state_path = tmp_path / "state/release.json"
    state = json.loads(state_path.read_bytes())
    state["required_absent_paths"] = [
        "dist/releases/g00000000/index.json",
    ]
    state_path.write_bytes(canonical_json_bytes(state))
    expected = replace(_raw_expectation(tmp_path), required_absent_paths=())

    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        return RawResponse(200, (tmp_path / relative).read_bytes())

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(ContractError, match="deletion receipt differs"):
        verifier.verify_revision("a" * 40, expected=expected)


def test_raw_rejects_manifest_path_escape_before_requesting_it() -> None:
    root = {
        "active_release_id": "g00000001",
        "release_manifest": {"path": "../../outside", "sha256": "sha256:" + "0" * 64},
        "aliases": {},
    }
    calls: list[str] = []

    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        calls.append(url)
        return RawResponse(200, canonical_json_bytes(root))

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(ContractError, match="active release manifest"):
        verifier.verify_revision("b" * 40)
    assert len(calls) == 1


def test_raw_rejects_unsupported_artifact_before_downloading_it(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    root = json.loads((tmp_path / "dist/manifest.json").read_bytes())
    release_path = root["release_manifest"]["path"]
    release = json.loads((tmp_path / release_path).read_bytes())
    release["artifacts"] = {
        "dist/releases/g00000001/../../escape.json": "sha256:" + "0" * 64
    }
    calls: list[str] = []

    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        calls.append(relative)
        if relative == "dist/manifest.json":
            return RawResponse(200, canonical_json_bytes(root))
        if relative == release_path:
            return RawResponse(200, canonical_json_bytes(release))
        raise AssertionError(f"unexpected download: {relative}")

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(ContractError, match="unsafe relative path"):
        verifier.verify_revision("c" * 40)
    assert calls == ["dist/manifest.json", release_path]


def test_polling_timeout_is_deterministic_and_preserves_last_error() -> None:
    current = 0.0
    attempts = 0

    def fetch(_url: str, _limit: int, _timeout: float) -> RawResponse:
        nonlocal attempts
        attempts += 1
        return RawResponse(503, b"")

    def sleep(seconds: float) -> None:
        nonlocal current
        current += seconds

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(PublishError, match="Raw returned 503"):
        verifier.poll_revision(
            "d" * 40,
            timeout_seconds=10,
            interval_seconds=5,
            sleeper=sleep,
            clock=lambda: current,
        )
    assert attempts == 2


def test_deleted_raw_paths_must_all_become_404_before_poll_succeeds() -> None:
    attempts: dict[str, int] = {}

    def fetch(url: str, limit: int, _timeout: float) -> RawResponse:
        assert limit == 64 * 1024
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        attempts[relative] = attempts.get(relative, 0) + 1
        if relative.endswith("index.json") and attempts[relative] == 1:
            return RawResponse(200, b"stale")
        return RawResponse(404, b"404: Not Found")

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    verifier.poll_absent(
        "generated",
        (
            "dist/releases/g00000001/index.json",
            "dist/releases/g00000001/health.json",
        ),
        timeout_seconds=1,
        interval_seconds=0,
        sleeper=lambda _seconds: None,
    )
    assert attempts == {
        "dist/releases/g00000001/health.json": 2,
        "dist/releases/g00000001/index.json": 2,
    }


def test_revision_and_absence_fetches_share_one_hard_deadline() -> None:
    current = 0.0
    observed_timeouts: list[float] = []

    def fetch(url: str, _limit: int, timeout: float) -> RawResponse:
        nonlocal current
        observed_timeouts.append(timeout)
        current += 0.6
        if url.endswith("dist/manifest.json"):
            return RawResponse(503, b"")
        return RawResponse(404, b"")

    verifier = RawVerifier("azhansy", "ds-tvbox", SCHEMAS, fetch=fetch)
    with pytest.raises(PublishError, match="timed out"):
        verifier.poll_revision(
            "d" * 40,
            timeout_seconds=0.5,
            interval_seconds=5,
            sleeper=lambda _seconds: None,
            clock=lambda: current,
        )
    assert observed_timeouts == [pytest.approx(0.5)]

    current = 0.0
    observed_timeouts.clear()
    with pytest.raises(PublishError, match="timed out"):
        verifier.poll_absent(
            "generated",
            (
                "dist/releases/g00000001/health.json",
                "dist/releases/g00000001/index.json",
            ),
            timeout_seconds=1,
            interval_seconds=5,
            sleeper=lambda _seconds: None,
            clock=lambda: current,
        )
    assert observed_timeouts == [pytest.approx(1.0), pytest.approx(0.4)]


def test_default_raw_fetch_forcibly_terminates_a_stalled_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stalled(_url: str, _limit: int, _timeout: float) -> RawResponse:
        time.sleep(2)
        return RawResponse(200, b"late")

    monkeypatch.setattr("ds_tvbox.raw._direct_raw_fetch", stalled)
    started = time.monotonic()
    with pytest.raises(PublishError, match="deadline expired"):
        default_raw_fetch(
            "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json",
            1024,
            0.05,
        )
    assert time.monotonic() - started < 0.5


def test_deleted_raw_receipt_is_nonempty_safe_and_requires_404() -> None:
    verifier = RawVerifier(
        "azhansy",
        "ds-tvbox",
        SCHEMAS,
        fetch=lambda _url, _limit, _timeout: RawResponse(410, b"gone"),
    )
    with pytest.raises(PublishError, match="receipt size"):
        verifier.verify_absent("generated", ())
    with pytest.raises(ContractError, match="unsafe relative path"):
        verifier.verify_absent("generated", ("../release.json",))
    with pytest.raises(PublishError, match="410 instead of 404"):
        verifier.verify_absent(
            "generated",
            ("dist/releases/g00000001/index.json",),
        )


def test_raw_revision_and_bare_ref_are_strictly_bounded() -> None:
    verifier = RawVerifier(
        "azhansy",
        "ds-tvbox",
        SCHEMAS,
        fetch=lambda _url, _limit, _timeout: RawResponse(404, b""),
    )
    with pytest.raises(PublishError, match="full commit SHA"):
        verifier.verify_revision("candidate/run-1-attempt-1")
    with pytest.raises(PublishError, match="restricted to generated"):
        verifier.poll_bare(ref="main", timeout_seconds=0)
    with pytest.raises(PublishError, match="bare raw.githubusercontent.com"):
        default_raw_fetch(
            "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json?run=1",
            1024,
        )

    oversized = RawVerifier(
        "azhansy",
        "ds-tvbox",
        SCHEMAS,
        fetch=lambda _url, limit, _timeout: RawResponse(200, b"x" * (limit + 1)),
    )
    with pytest.raises(PublishError, match="response exceeds"):
        oversized.verify_revision("e" * 40)


def test_raw_owner_repository_and_paths_are_strict() -> None:
    with pytest.raises(PublishError, match="unsupported characters"):
        RawVerifier("bad/owner", "repo", SCHEMAS)
    with pytest.raises(ContractError, match="not canonical POSIX"):
        RawVerifier._safe_relative("dist//index.json")
    with pytest.raises(ContractError, match="crosses the active release"):
        RawVerifier._validate_release_artifact(
            "dist/releases/g00000002/index.json", "g00000001"
        )
    with pytest.raises(ContractError, match="path is unsupported"):
        RawVerifier._validate_release_artifact(
            "dist/releases/g00000001/debug.txt", "g00000001"
        )


@pytest.mark.parametrize("body", [b"{", b"[]", b"\xff"])
def test_raw_json_decoder_requires_utf8_object(body: bytes) -> None:
    error = "root must be object" if body == b"[]" else "invalid Raw JSON"
    with pytest.raises(ContractError, match=error):
        RawVerifier._decode_json(body, "fixture")


def _fetch_documents(
    documents: dict[str, bytes],
    calls: list[str] | None = None,
) -> Callable[[str, int, float], RawResponse]:
    def fetch(url: str, _limit: int, _timeout: float) -> RawResponse:
        relative = unquote(urlsplit(url).path).split("/", 4)[4]
        if calls is not None:
            calls.append(relative)
        if relative not in documents:
            raise AssertionError(f"unexpected Raw path: {relative}")
        return RawResponse(200, documents[relative])

    return fetch


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value.update(active_release_id="release-1"), "active_release_id"),
        (lambda value: value.update(release_manifest=[]), "invalid pointers"),
        (lambda value: value.update(aliases=[]), "invalid pointers"),
        (
            lambda value: value["release_manifest"].update(path="dist/manifest.json"),
            "not the active release manifest",
        ),
        (lambda value: value.update(aliases={}), "exact alias set"),
    ],
)
def test_raw_root_manifest_shape_is_rechecked_before_following_paths(
    tmp_path: Path,
    mutate: Any,
    error: str,
) -> None:
    _release_tree(tmp_path)
    root = json.loads((tmp_path / "dist/manifest.json").read_bytes())
    mutate(root)
    calls: list[str] = []
    verifier = RawVerifier(
        "azhansy",
        "ds-tvbox",
        SCHEMAS,
        fetch=_fetch_documents(
            {"dist/manifest.json": canonical_json_bytes(root)}, calls
        ),
    )
    with pytest.raises(ContractError, match=error):
        verifier.verify_revision("f" * 40)
    assert calls == ["dist/manifest.json"]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value.update(release_id="g00000002"), "ID differs"),
        (lambda value: value.update(artifacts=[]), "artifacts missing"),
        (
            lambda value: value.update(
                artifacts={
                    f"dist/releases/g00000001/configs/x-{index}.json": "x"
                    for index in range(10_001)
                }
            ),
            "count exceeds",
        ),
        (lambda value: value.update(artifacts={"1": "x"}), "crosses the active release"),
    ],
)
def test_raw_release_manifest_shape_is_checked_before_artifact_downloads(
    tmp_path: Path,
    mutate: Any,
    error: str,
) -> None:
    _release_tree(tmp_path)
    root_bytes = (tmp_path / "dist/manifest.json").read_bytes()
    root = json.loads(root_bytes)
    release_path = root["release_manifest"]["path"]
    release = json.loads((tmp_path / release_path).read_bytes())
    mutate(release)
    verifier = RawVerifier(
        "azhansy",
        "ds-tvbox",
        SCHEMAS,
        fetch=_fetch_documents(
            {
                "dist/manifest.json": root_bytes,
                release_path: canonical_json_bytes(release),
            }
        ),
    )
    with pytest.raises(ContractError, match=error):
        verifier.verify_revision("a" * 40)


def test_polling_rejects_negative_intervals_and_recovers_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = RawVerifier(
        "azhansy",
        "ds-tvbox",
        SCHEMAS,
        fetch=lambda _url, _limit, _timeout: RawResponse(404, b""),
    )
    with pytest.raises(PublishError, match="cannot be negative"):
        verifier.poll_revision("a" * 40, timeout_seconds=-1)

    attempts: list[tuple[str, str]] = []

    def verify(
        revision: str,
        expected_status: str = "success",
        *,
        expected: RawExpectedRelease | None = None,
        deadline: float | None = None,
        clock: Callable[[], float],
    ) -> None:
        assert expected is None
        assert deadline == 5
        assert callable(clock)
        attempts.append((revision, expected_status))
        if len(attempts) == 1:
            raise ContractError("not visible yet")

    monkeypatch.setattr(verifier, "verify_revision", verify)
    clock_values = iter((0.0, 0.0, 1.0))
    verifier.poll_revision(
        "b" * 40,
        timeout_seconds=5,
        interval_seconds=0,
        sleeper=lambda _seconds: None,
        clock=lambda: next(clock_values),
        expected_status="pending",
    )
    assert attempts == [("b" * 40, "pending"), ("b" * 40, "pending")]


def test_default_raw_fetch_uses_bounded_response_and_maps_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _amount: int) -> bytes:
            return b"ok"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    response = default_raw_fetch(
        "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json",
        2,
    )
    assert response == RawResponse(200, b"ok")

    class Oversized(Response):
        def read(self, amount: int) -> bytes:
            return b"x" * amount

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Oversized())
    with pytest.raises(PublishError, match="response exceeds"):
        default_raw_fetch(
            "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json",
            2,
        )

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    with pytest.raises(PublishError, match="Raw request failed: URLError"):
        default_raw_fetch(
            "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json",
            2,
        )


def test_default_raw_fetch_preserves_bounded_http_404_for_deletion_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/missing.json"

    def missing(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(
            url,
            404,
            "Not Found",
            hdrs=None,
            fp=io.BytesIO(b"404: Not Found"),
        )

    monkeypatch.setattr("urllib.request.urlopen", missing)
    assert default_raw_fetch(url, 64 * 1024) == RawResponse(404, b"404: Not Found")

    def oversized(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(
            url,
            404,
            "Not Found",
            hdrs=None,
            fp=io.BytesIO(b"too large"),
        )

    monkeypatch.setattr("urllib.request.urlopen", oversized)
    with pytest.raises(PublishError, match="response exceeds"):
        default_raw_fetch(url, 2)
