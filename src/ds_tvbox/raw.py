"""Commit and bare Raw delivery verification for GitHub-hosted releases."""

from __future__ import annotations

import json
import multiprocessing
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import MappingProxyType
from urllib.parse import quote, urlsplit

from ds_tvbox.bundle import validate_bundle
from ds_tvbox.errors import ContractError, PublishError
from ds_tvbox.manifests import verify_hash
from ds_tvbox.serialization import ensure_relative_safe_path, write_bytes
from ds_tvbox.validation import validate_release_tree

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID = re.compile(r"^g[0-9]{8}$")
_GITHUB_COMPONENT = re.compile(r"^[0-9A-Za-z_.-]+$")
_ROOT_ALIASES = frozenset(
    {
        "dist/index.json",
        "dist/warehouse.json",
        "dist/configs/stable.json",
        "dist/live/stable.m3u",
        "dist/health.json",
    }
)
_FIXED_RELEASE_ARTIFACTS = frozenset(
    {
        "index.json",
        "warehouse.json",
        "depots/stable.json",
        "depots/public-unverified.json",
        "configs/stable.json",
        "live/stable.m3u",
        "health.json",
    }
)
_SOURCE_CONFIG = re.compile(r"^configs/[a-z0-9]+(?:-[a-z0-9]+)*\.json$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class RawResponse:
    status: int
    body: bytes


RawFetch = Callable[[str, int, float], RawResponse]


@dataclass(frozen=True)
class RawExpectedRelease:
    """Caller-sealed identity that prevents accepting a stale but valid Raw release."""

    release_id: str
    release_generation: int
    event_generation: int
    workflow_run_id: str
    workflow_run_attempt: int
    content_workflow_run_id: str
    content_workflow_run_attempt: int
    root_manifest_sha256: str
    release_manifest_sha256: str
    aliases: Mapping[str, str]
    required_absent_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _RELEASE_ID.fullmatch(self.release_id):
            raise PublishError("Raw expected release ID is invalid")
        if self.release_generation < 1 or self.event_generation < self.release_generation:
            raise PublishError("Raw expected generations are invalid")
        if (
            not self.workflow_run_id
            or self.workflow_run_attempt < 1
            or not self.content_workflow_run_id
            or self.content_workflow_run_attempt < 1
        ):
            raise PublishError("Raw expected workflow identity is invalid")
        if not _HASH.fullmatch(self.root_manifest_sha256) or not _HASH.fullmatch(
            self.release_manifest_sha256
        ):
            raise PublishError("Raw expected manifest hash is invalid")
        aliases = dict(self.aliases)
        if set(aliases) != set(_ROOT_ALIASES) or any(
            not isinstance(value, str) or not _HASH.fullmatch(value)
            for value in aliases.values()
        ):
            raise PublishError("Raw expected alias identity is invalid")
        object.__setattr__(self, "aliases", MappingProxyType(aliases))
        receipt = self.required_absent_paths
        if (
            len(receipt) > 10_000
            or receipt != tuple(sorted(set(receipt)))
            or any(
                not isinstance(relative, str)
                or PurePosixPath(relative).as_posix() != relative
                or len(PurePosixPath(relative).parts) < 4
                or PurePosixPath(relative).parts[:2] != ("dist", "releases")
                or not _RELEASE_ID.fullmatch(PurePosixPath(relative).parts[2])
                for relative in receipt
            )
        ):
            raise PublishError("Raw expected deletion receipt is invalid")
        for relative in receipt:
            ensure_relative_safe_path(relative)


def _direct_raw_fetch(url: str, limit: int, timeout_seconds: float) -> RawResponse:
    request = urllib.request.Request(  # noqa: S310 - scheme and host are fixed by caller
        url,
        headers={"User-Agent": "ds-tvbox-delivery-verifier/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme and host are fixed by caller
            request,
            timeout=timeout_seconds,
        ) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise PublishError(f"Raw response exceeds {limit} bytes")
            return RawResponse(status=response.status, body=body)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(limit + 1)
        except OSError as read_error:
            raise PublishError("Raw HTTP error response could not be read") from read_error
        finally:
            error.close()
        if len(body) > limit:
            raise PublishError(f"Raw response exceeds {limit} bytes") from error
        return RawResponse(status=error.code, body=body)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PublishError(f"Raw request failed: {type(error).__name__}") from error


def _raw_fetch_process(
    connection: object,
    url: str,
    limit: int,
    timeout_seconds: float,
) -> None:
    """Child boundary that makes resolver stalls forcibly terminable."""

    sender = connection
    try:
        response = _direct_raw_fetch(url, limit, timeout_seconds)
        sender.send(("ok", response.status, response.body))  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 - serialize a stable child-process failure
        sender.send(("error", str(error), b""))  # type: ignore[attr-defined]
    finally:
        sender.close()  # type: ignore[attr-defined]


def default_raw_fetch(
    url: str,
    limit: int,
    timeout_seconds: float = 15.0,
) -> RawResponse:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "raw.githubusercontent.com"
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise PublishError("Raw verifier accepts only bare raw.githubusercontent.com HTTPS URLs")
    if timeout_seconds <= 0:
        raise PublishError("Raw request deadline expired")
    methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in methods else "spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(  # type: ignore[attr-defined]
        target=_raw_fetch_process,
        args=(sender, url, limit, timeout_seconds),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            raise PublishError("Raw request deadline expired")
        result = receiver.recv()
    except EOFError as error:
        raise PublishError("Raw request worker exited without a result") from error
    finally:
        receiver.close()
        process.join(0.02)
        if process.is_alive():
            process.terminate()
            process.join(0.02)
        if process.is_alive():
            process.kill()
            process.join(0.02)
    if result[0] == "error":
        raise PublishError(str(result[1]))
    return RawResponse(status=int(result[1]), body=bytes(result[2]))


class RawVerifier:
    def __init__(
        self,
        owner: str,
        repository: str,
        schema_root: Path,
        fetch: RawFetch = default_raw_fetch,
    ) -> None:
        if not _GITHUB_COMPONENT.fullmatch(owner) or not _GITHUB_COMPONENT.fullmatch(repository):
            raise PublishError("Raw owner/repository contains unsupported characters")
        self.owner = owner
        self.repository = repository
        self.schema_root = schema_root
        self.fetch = fetch

    @staticmethod
    def _validate_revision(revision: str) -> str:
        if revision != "generated" and not _SHA.fullmatch(revision):
            raise PublishError("Raw revision must be generated or a full commit SHA")
        return revision

    @staticmethod
    def _safe_relative(relative: str) -> str:
        if PurePosixPath(relative).as_posix() != relative:
            raise ContractError(f"Raw path is not canonical POSIX: {relative!r}")
        ensure_relative_safe_path(relative)
        return relative

    @staticmethod
    def _validate_release_artifact(relative: str, release_id: str) -> str:
        RawVerifier._safe_relative(relative)
        prefix = f"dist/releases/{release_id}/"
        if not relative.startswith(prefix):
            raise ContractError("Raw release artifact crosses the active release")
        local = relative.removeprefix(prefix)
        if local not in _FIXED_RELEASE_ARTIFACTS and not (
            _SOURCE_CONFIG.fullmatch(local) and local != "configs/stable.json"
        ):
            raise ContractError(f"Raw release artifact path is unsupported: {relative}")
        return relative

    def _url(self, revision: str, relative: str) -> str:
        revision = self._validate_revision(revision)
        relative = self._safe_relative(relative)
        encoded = "/".join(quote(part, safe="") for part in relative.split("/"))
        return (
            f"https://raw.githubusercontent.com/{quote(self.owner, safe='')}/"
            f"{quote(self.repository, safe='')}/{quote(revision, safe='')}/{encoded}"
        )

    @staticmethod
    def _remaining(
        deadline: float | None,
        clock: Callable[[], float],
    ) -> float:
        return 15.0 if deadline is None else max(0.0, deadline - clock())

    def _download(
        self,
        revision: str,
        relative: str,
        limit: int = 10 * 1024 * 1024,
        *,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> bytes:
        remaining = self._remaining(deadline, clock)
        response = self.fetch(self._url(revision, relative), limit, remaining)
        if deadline is not None and clock() > deadline:
            raise PublishError("Raw request exceeded the shared deadline")
        if response.status != 200:
            raise PublishError(f"Raw returned {response.status} for {relative}")
        if len(response.body) > limit:
            raise PublishError(f"Raw response exceeds {limit} bytes for {relative}")
        return response.body

    @staticmethod
    def _decode_json(data: bytes, name: str) -> Mapping[str, object]:
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError(f"invalid Raw JSON: {name}") from error
        if not isinstance(value, dict):
            raise ContractError(f"Raw JSON root must be object: {name}")
        return value

    def verify_revision(
        self,
        revision: str,
        expected_status: str = "success",
        *,
        expected: RawExpectedRelease | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        revision = self._validate_revision(revision)
        manifest_bytes = self._download(
            revision,
            "dist/manifest.json",
            1024 * 1024,
            deadline=deadline,
            clock=clock,
        )
        if expected is not None:
            verify_hash(
                expected.root_manifest_sha256,
                manifest_bytes,
                label="Raw dist/manifest.json",
            )
        root_manifest = self._decode_json(manifest_bytes, "dist/manifest.json")
        release_id = root_manifest.get("active_release_id")
        if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
            raise ContractError("Raw root manifest has invalid active_release_id")
        if expected is not None and release_id != expected.release_id:
            raise ContractError("Raw active release differs from the sealed expectation")
        release_pointer = root_manifest.get("release_manifest")
        aliases = root_manifest.get("aliases")
        if not isinstance(release_pointer, dict) or not isinstance(aliases, dict):
            raise ContractError("Raw root manifest has invalid pointers")
        release_path = release_pointer.get("path")
        expected_release_path = f"dist/releases/{release_id}/manifest.json"
        if release_path != expected_release_path:
            raise ContractError("Raw release manifest path is not the active release manifest")
        self._safe_relative(expected_release_path)
        if set(aliases) != set(_ROOT_ALIASES):
            raise ContractError("Raw root manifest does not contain the exact alias set")
        if expected is not None and aliases != dict(expected.aliases):
            raise ContractError("Raw aliases differ from the sealed expectation")
        for alias in aliases:
            if not isinstance(alias, str):
                raise ContractError("Raw alias path must be a string")
            self._safe_relative(alias)
        release_bytes = self._download(
            revision,
            release_path,
            2 * 1024 * 1024,
            deadline=deadline,
            clock=clock,
        )
        if expected is not None:
            if release_pointer.get("sha256") != expected.release_manifest_sha256:
                raise ContractError("Raw release pointer differs from the sealed expectation")
            verify_hash(
                expected.release_manifest_sha256,
                release_bytes,
                label=f"Raw {release_path}",
            )
        release_manifest = self._decode_json(release_bytes, release_path)
        if release_manifest.get("release_id") != release_id:
            raise ContractError("Raw release manifest ID differs from the root manifest")
        if expected is not None:
            if release_manifest.get("generation") != expected.release_generation:
                raise ContractError("Raw release generation differs from the sealed expectation")
            content_identity = (
                expected.content_workflow_run_id,
                expected.content_workflow_run_attempt,
            )
            if (
                root_manifest.get("content_workflow_run_id"),
                root_manifest.get("content_workflow_run_attempt"),
            ) != content_identity or (
                release_manifest.get("content_workflow_run_id"),
                release_manifest.get("content_workflow_run_attempt"),
            ) != content_identity:
                raise ContractError(
                    "Raw manifest content identity differs from the sealed expectation"
                )
        artifacts = release_manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ContractError("Raw release artifacts missing")
        if len(artifacts) > 10_000:
            raise ContractError("Raw release artifact count exceeds the verifier limit")
        for artifact_path in artifacts:
            if not isinstance(artifact_path, str):
                raise ContractError("Raw artifact path must be a string")
            self._validate_release_artifact(artifact_path, release_id)

        paths = {
            "dist/manifest.json",
            "state/release.json",
            "dist/reports/latest.json",
            "dist/reports/latest.md",
            release_path,
            *[str(path) for path in aliases],
            *[str(path) for path in artifacts],
        }
        with TemporaryDirectory(prefix="ds-tvbox-raw-") as temporary:
            root = Path(temporary)
            write_bytes(root / "dist/manifest.json", manifest_bytes)
            write_bytes(root / release_path, release_bytes)
            for relative in sorted(paths - {"dist/manifest.json", release_path}):
                write_bytes(
                    root / relative,
                    self._download(
                        revision,
                        relative,
                        deadline=deadline,
                        clock=clock,
                    ),
                )
            validate_bundle(
                root,
                schemas_dir=self.schema_root,
                expected_release_id=release_id,
            )
            validate_release_tree(
                root,
                self.schema_root,
                owner=self.owner,
                repository=self.repository,
                expected_status=expected_status,
            )
            if expected is not None:
                state = self._decode_json(
                    (root / "state/release.json").read_bytes(), "state/release.json"
                )
                report = self._decode_json(
                    (root / "dist/reports/latest.json").read_bytes(),
                    "dist/reports/latest.json",
                )
                event_identity = (
                    expected.workflow_run_id,
                    expected.workflow_run_attempt,
                )
                for label, document in (("state", state), ("report", report)):
                    if document.get("active_release_id") != expected.release_id:
                        raise ContractError(
                            f"Raw {label} release differs from the sealed expectation"
                        )
                    if document.get("generation") != expected.event_generation:
                        raise ContractError(
                            f"Raw {label} generation differs from the sealed expectation"
                        )
                    if (
                        document.get("workflow_run_id"),
                        document.get("workflow_run_attempt"),
                    ) != event_identity:
                        raise ContractError(
                            f"Raw {label} event differs from the sealed expectation"
                        )
                raw_receipt = state.get("required_absent_paths", [])
                if raw_receipt != list(expected.required_absent_paths):
                    raise ContractError(
                        "Raw state deletion receipt differs from the sealed expectation"
                    )

    def poll_bare(
        self,
        *,
        ref: str = "generated",
        timeout_seconds: int = 120,
        interval_seconds: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        expected: RawExpectedRelease | None = None,
    ) -> None:
        if ref != "generated":
            raise PublishError("bare Raw verification is restricted to generated")
        self.poll_revision(
            ref,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            sleeper=sleeper,
            clock=clock,
            expected=expected,
        )

    def poll_revision(
        self,
        revision: str,
        *,
        timeout_seconds: int = 120,
        interval_seconds: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        expected_status: str = "success",
        expected: RawExpectedRelease | None = None,
    ) -> None:
        if timeout_seconds < 0 or interval_seconds < 0:
            raise PublishError("Raw polling intervals cannot be negative")
        deadline = clock() + timeout_seconds
        last_error: Exception | None = None
        while True:
            if last_error is not None and clock() >= deadline:
                raise PublishError(
                    f"Raw verification timed out for {revision}: {last_error}"
                ) from last_error
            try:
                self.verify_revision(
                    revision,
                    expected_status=expected_status,
                    expected=expected,
                    deadline=deadline,
                    clock=clock,
                )
                return
            except (ContractError, PublishError) as error:
                last_error = error
            remaining = deadline - clock()
            if remaining <= 0:
                raise PublishError(
                    f"Raw verification timed out for {revision}: {last_error}"
                ) from last_error
            sleeper(min(float(interval_seconds), remaining))

    def verify_absent(
        self,
        revision: str,
        relatives: tuple[str, ...],
        *,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Require every previously published file to be a bare Raw 404."""

        revision = self._validate_revision(revision)
        if not relatives or len(relatives) > 10_000:
            raise PublishError("Raw deletion receipt size is invalid")
        for relative in sorted(set(relatives)):
            relative = self._safe_relative(relative)
            response = self.fetch(
                self._url(revision, relative),
                64 * 1024,
                self._remaining(deadline, clock),
            )
            if deadline is not None and clock() > deadline:
                raise PublishError("Raw request exceeded the shared deadline")
            if response.status != 404:
                raise PublishError(
                    f"Raw deleted path returned {response.status} instead of 404: {relative}"
                )

    def poll_absent(
        self,
        revision: str,
        relatives: tuple[str, ...],
        *,
        timeout_seconds: int = 120,
        interval_seconds: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds < 0 or interval_seconds < 0:
            raise PublishError("Raw polling intervals cannot be negative")
        deadline = clock() + timeout_seconds
        last_error: Exception | None = None
        while True:
            if last_error is not None and clock() >= deadline:
                raise PublishError(
                    f"Raw deletion verification timed out for {revision}: {last_error}"
                ) from last_error
            try:
                self.verify_absent(
                    revision,
                    relatives,
                    deadline=deadline,
                    clock=clock,
                )
                return
            except (ContractError, PublishError) as error:
                last_error = error
            remaining = deadline - clock()
            if remaining <= 0:
                raise PublishError(
                    f"Raw deletion verification timed out for {revision}: {last_error}"
                ) from last_error
            sleeper(min(float(interval_seconds), remaining))
