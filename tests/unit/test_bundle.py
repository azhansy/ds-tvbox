from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ds_tvbox.bundle import build_bundle_files, materialize_bundle, validate_bundle
from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.generator import build_client_artifacts
from ds_tvbox.manifests import prefixed_sha256
from ds_tvbox.models import ReleaseKind, RunContext
from ds_tvbox.reports import build_latest_report, render_latest_markdown
from ds_tvbox.serialization import canonical_json_bytes


def _context() -> RunContext:
    return RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="12345",
        workflow_run_attempt=1,
        generated_at="2026-07-22T12:00:00Z",
        generation=1,
        release_kind=ReleaseKind.BOOTSTRAP,
        previous_head=None,
        previous_last_success_at=None,
    )


def _health() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-22T12:00:00Z",
        "generation": 1,
        "release_id": "g00000001",
        "sources": [],
        "channels": [],
    }


def _client():
    return build_client_artifacts(
        context=_context(),
        sources=[],
        vod_results=[],
        channels=[],
    )


def _client_with_site_url(api: str):
    client = _client()
    relative = "dist/releases/g00000001/configs/stable.json"
    document = json.loads(client.release_files[relative])
    document["sites"] = [
        {
            "key": "mutated",
            "name": "Mutated",
            "type": 1,
            "api": api,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 0,
            "changeable": 0,
        }
    ]
    data = canonical_json_bytes(document)
    release_files = dict(client.release_files)
    release_files[relative] = data
    alias_files = dict(client.alias_files)
    alias_files["dist/configs/stable.json"] = data
    return replace(client, release_files=release_files, alias_files=alias_files)


def test_valid_bundle_reopens_schemas_hashes_aliases_and_closure(tmp_path) -> None:
    bundle = build_bundle_files(
        context=_context(),
        client_artifacts=_client(),
        health=_health(),
        source_count=0,
    )
    materialize_bundle(tmp_path, bundle)

    result = validate_bundle(tmp_path, expected_release_id="g00000001")

    assert result.release_id == "g00000001"
    assert result.generation == 1
    assert result.artifact_count == 7
    assert result.alias_count == 5
    assert result.release_manifest_sha256 == bundle.release_manifest_sha256
    assert result.root_manifest_sha256 == bundle.root_manifest_sha256


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "foreign"},
        {"repository": "foreign"},
        {"generated_ref": "main"},
    ],
)
def test_bundle_builder_rejects_foreign_owner_repository_or_ref(
    change: dict[str, str],
) -> None:
    context = replace(_context(), **change)
    client = build_client_artifacts(
        context=context,
        sources=[],
        vod_results=[],
        channels=[],
    )

    with pytest.raises(ContractError, match="trusted azhansy/ds-tvbox/generated"):
        build_bundle_files(
            context=context,
            client_artifacts=client,
            health=_health(),
            source_count=0,
        )


def test_bundle_rejects_hash_tampering(tmp_path) -> None:
    bundle = build_bundle_files(
        context=_context(), client_artifacts=_client(), health=_health(), source_count=0
    )
    materialize_bundle(tmp_path, bundle)
    path = tmp_path / "dist/releases/g00000001/index.json"
    path.write_bytes(b'{"urls":[]}\n')

    with pytest.raises(ContractError, match="hash mismatch"):
        validate_bundle(tmp_path)


def test_bundle_rejects_schema_invalid_client_document_even_when_hashes_match(tmp_path) -> None:
    client = _client()
    invalid = canonical_json_bytes(
        {"sites": [], "lives": [], "parses": [], "spider": "https://evil.test/a.jar"}
    )
    release_files = dict(client.release_files)
    release_files["dist/releases/g00000001/configs/stable.json"] = invalid
    alias_files = dict(client.alias_files)
    alias_files["dist/configs/stable.json"] = invalid
    bad_client = replace(client, release_files=release_files, alias_files=alias_files)
    bundle = build_bundle_files(
        context=_context(),
        client_artifacts=bad_client,
        health=_health(),
        source_count=0,
    )
    materialize_bundle(tmp_path, bundle)

    with pytest.raises(ContractError, match="Schema validation failed"):
        validate_bundle(tmp_path)


@pytest.mark.parametrize(
    "api",
    [
        "https://example.test/api?to_ken=visible",
        "https://example.test/api?ｔｏ＿ｋｅｎ=visible",
        "https://127.0.0.1/api",
        "https://[::1]/api",
    ],
)
def test_bundle_rejects_rehashed_unsafe_client_url(tmp_path: Path, api: str) -> None:
    bundle = build_bundle_files(
        context=_context(),
        client_artifacts=_client_with_site_url(api),
        health=_health(),
        source_count=0,
    )
    materialize_bundle(tmp_path, bundle)

    with pytest.raises(SecurityError, match="credential-free HTTPS"):
        validate_bundle(tmp_path)


def test_bundle_rejects_cross_generation_index_even_when_hashes_match(tmp_path) -> None:
    client = _client()
    release_files = dict(client.release_files)
    index_path = "dist/releases/g00000001/index.json"
    index = json.loads(release_files[index_path])
    index["urls"][0]["url"] = index["urls"][0]["url"].replace("g00000001", "g00000002")
    crossed = canonical_json_bytes(index)
    release_files[index_path] = crossed
    alias_files = dict(client.alias_files)
    alias_files["dist/index.json"] = crossed
    bad_client = replace(client, release_files=release_files, alias_files=alias_files)
    bundle = build_bundle_files(
        context=_context(),
        client_artifacts=bad_client,
        health=_health(),
        source_count=0,
    )
    materialize_bundle(tmp_path, bundle)

    with pytest.raises(ContractError, match="trusted.*release"):
        validate_bundle(tmp_path)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/azhansy/", "/foreign/"),
        ("/ds-tvbox/", "/foreign/"),
        ("/generated/", "/main/"),
    ],
)
def test_bundle_rejects_fully_rehashed_foreign_raw_identity(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    client = _client()
    release_files = dict(client.release_files)
    index_path = "dist/releases/g00000001/index.json"
    index = json.loads(release_files[index_path])
    index["urls"][0]["url"] = index["urls"][0]["url"].replace(old, new)
    changed = canonical_json_bytes(index)
    release_files[index_path] = changed
    alias_files = dict(client.alias_files)
    alias_files["dist/index.json"] = changed
    bundle = build_bundle_files(
        context=_context(),
        client_artifacts=replace(
            client,
            release_files=release_files,
            alias_files=alias_files,
        ),
        health=_health(),
        source_count=0,
    )
    materialize_bundle(tmp_path, bundle)

    with pytest.raises(ContractError, match="trusted azhansy/ds-tvbox/generated"):
        validate_bundle(tmp_path)


def test_materialization_refuses_release_overwrite_and_unsafe_paths(tmp_path) -> None:
    bundle = build_bundle_files(
        context=_context(), client_artifacts=_client(), health=_health(), source_count=0
    )
    materialize_bundle(tmp_path, bundle)

    old_index = (tmp_path / "dist/index.json").read_bytes()
    changed = dict(bundle.files)
    changed["dist/index.json"] = b"must not be partially written"
    with pytest.raises(ContractError, match="immutable release"):
        materialize_bundle(tmp_path, changed)
    assert (tmp_path / "dist/index.json").read_bytes() == old_index
    with pytest.raises(ContractError, match="unsafe relative path"):
        materialize_bundle(tmp_path, {"../escape": b"no"})


def test_bundle_rejects_manifest_declared_debug_artifact(tmp_path) -> None:
    bundle = build_bundle_files(
        context=_context(), client_artifacts=_client(), health=_health(), source_count=0
    )
    materialize_bundle(tmp_path, bundle)
    debug_path = tmp_path / "dist/releases/g00000001/debug.txt"
    debug_path.write_bytes(b"debug")
    release_manifest_path = tmp_path / "dist/releases/g00000001/manifest.json"
    release_manifest = json.loads(release_manifest_path.read_bytes())
    release_manifest["artifacts"]["dist/releases/g00000001/debug.txt"] = prefixed_sha256(b"debug")
    release_bytes = canonical_json_bytes(release_manifest)
    release_manifest_path.write_bytes(release_bytes)
    root_manifest_path = tmp_path / "dist/manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    root_manifest["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    root_manifest_path.write_bytes(canonical_json_bytes(root_manifest))

    with pytest.raises(ContractError, match="unsupported artifact"):
        validate_bundle(tmp_path)


def test_pending_state_and_reports_obey_event_and_content_identity(tmp_path) -> None:
    context = _context()
    content_identity = {
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
    }
    report = build_latest_report(
        context,
        status="pending",
        started_at=context.generated_at,
        finished_at=context.generated_at,
        due=False,
        forced=True,
        recovery_due=False,
        sources=[],
        counts={},
        gate={},
        previous_release_head_sha=None,
        candidate_ref=context.candidate_ref,
        content_identity=content_identity,
    )
    state = {
        "schema_version": "1.0.0",
        "status": "pending",
        "release_kind": "bootstrap",
        "generation": 1,
        "active_release_id": "g00000001",
        "last_publish_at": None,
        "last_success_at": None,
        "content_commit_sha": None,
        "previous_release_head_sha": None,
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
    }
    bundle = build_bundle_files(
        context=context,
        client_artifacts=_client(),
        health=_health(),
        source_count=0,
        supplemental_files={
            "state/release.json": canonical_json_bytes(state),
            "dist/reports/latest.json": canonical_json_bytes(report),
            "dist/reports/latest.md": render_latest_markdown(report),
        },
    )
    materialize_bundle(tmp_path, bundle)

    assert validate_bundle(tmp_path).release_id == "g00000001"


def _tree(root: Path) -> None:
    bundle = build_bundle_files(
        context=_context(), client_artifacts=_client(), health=_health(), source_count=0
    )
    materialize_bundle(root, bundle)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: object) -> bytes:
    data = canonical_json_bytes(value)
    path.write_bytes(data)
    return data


def _rewrite_release_manifest(root: Path, mutate: Any) -> None:
    root_path = root / "dist/manifest.json"
    root_manifest = _json(root_path)
    release_path = root / str(root_manifest["release_manifest"]["path"])
    release = _json(release_path)
    mutate(release)
    release_bytes = _write(release_path, release)
    root_manifest["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    _write(root_path, root_manifest)


def _rewrite_release_artifact(
    root: Path,
    relative: str,
    value: object | bytes,
    *,
    alias: str | None = None,
) -> None:
    data = value if isinstance(value, bytes) else canonical_json_bytes(value)
    (root / relative).write_bytes(data)
    root_path = root / "dist/manifest.json"
    root_manifest = _json(root_path)
    release_path = root / str(root_manifest["release_manifest"]["path"])
    release = _json(release_path)
    release["artifacts"][relative] = prefixed_sha256(data)
    release_bytes = _write(release_path, release)
    root_manifest["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    if alias is not None:
        (root / alias).write_bytes(data)
        root_manifest["aliases"][alias] = prefixed_sha256(data)
    _write(root_path, root_manifest)


def test_bundle_builder_rejects_mismatched_inputs_and_supplemental_code(tmp_path) -> None:
    client = replace(_client(), release_id="g00000002")
    with pytest.raises(ContractError, match="different releases"):
        build_bundle_files(
            context=_context(), client_artifacts=client, health=_health(), source_count=0
        )
    with pytest.raises(ContractError, match="invalid JSON in health"):
        build_bundle_files(
            context=_context(), client_artifacts=_client(), health=b"{", source_count=0
        )
    with pytest.raises(ContractError, match="non-canonical JSON in health"):
        build_bundle_files(
            context=_context(),
            client_artifacts=_client(),
            health=b'{"schema_version": "1.0.0"}',
            source_count=0,
        )
    with pytest.raises(ContractError, match="unsupported supplemental"):
        build_bundle_files(
            context=_context(),
            client_artifacts=_client(),
            health=_health(),
            source_count=0,
            supplemental_files={"src/plugin.py": b"bad"},
        )
    with pytest.raises(ContractError, match="is not bytes"):
        build_bundle_files(
            context=_context(),
            client_artifacts=_client(),
            health=_health(),
            source_count=0,
            supplemental_files={"state/release.json": "bad"},  # type: ignore[dict-item]
        )


def test_materializer_rejects_root_and_component_symlinks_and_nonbytes(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SecurityError, match="root must not be a symlink"):
        materialize_bundle(root_link, {"dist/index.json": b"{}\n"})

    root = tmp_path / "tree"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "dist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityError, match="traverses symlink"):
        materialize_bundle(root, {"dist/index.json": b"{}\n"})

    with pytest.raises(ContractError, match="not bytes"):
        materialize_bundle(tmp_path / "bad-bytes", {"dist/index.json": "bad"})  # type: ignore[dict-item]
    with pytest.raises(ContractError, match="not canonical POSIX"):
        materialize_bundle(tmp_path / "bad-path", {"dist//index.json": b"bad"})


def test_materializer_may_reuse_only_byte_identical_immutable_files(tmp_path) -> None:
    _tree(tmp_path)
    original = (tmp_path / "dist/releases/g00000001/index.json").read_bytes()
    materialize_bundle(
        tmp_path,
        {"dist/releases/g00000001/index.json": original},
        refuse_existing_release_files=False,
    )
    with pytest.raises(ContractError, match="immutable release"):
        materialize_bundle(
            tmp_path,
            {"dist/releases/g00000001/index.json": b"different"},
            refuse_existing_release_files=False,
        )


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value.pop("artifacts"), "release manifest keys invalid"),
        (lambda value: value.update(schema_version="2.0.0"), "unsupported manifest"),
        (
            lambda value: value.update(previous_commit_sha="a" * 40),
            "content metadata differ",
        ),
        (
            lambda value: value.update(content_workflow_run_id="different"),
            "content identities differ",
        ),
        (lambda value: value.update(generation=True), "generation and release_id differ"),
        (lambda value: value.update(source_count=-1), "counts are invalid"),
        (lambda value: value.update(upstreams={}), "upstream records differ"),
    ],
)
def test_rehashed_release_manifest_tampering_is_rejected(
    tmp_path: Path,
    mutate: Any,
    error: str,
) -> None:
    _tree(tmp_path)
    _rewrite_release_manifest(tmp_path, mutate)
    with pytest.raises(ContractError, match=error):
        validate_bundle(tmp_path)


def test_bundle_rejects_root_pointer_alias_and_release_directory_drift(tmp_path) -> None:
    _tree(tmp_path)
    root_path = tmp_path / "dist/manifest.json"
    root = _json(root_path)
    root["release_manifest"] = []
    _write(root_path, root)
    with pytest.raises(ContractError, match="pointer path is invalid"):
        validate_bundle(tmp_path)

    _tree(tmp_path := tmp_path / "aliases")
    root_path = tmp_path / "dist/manifest.json"
    root = _json(root_path)
    root["aliases"].pop("dist/index.json")
    _write(root_path, root)
    with pytest.raises(ContractError, match="exact alias set"):
        validate_bundle(tmp_path)

    _tree(tmp_path := tmp_path / "untracked")
    (tmp_path / "dist/releases/g00000001/untracked.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError, match="untracked or missing"):
        validate_bundle(tmp_path)


def test_bundle_rejects_matching_but_invalid_manifest_content_identity(tmp_path) -> None:
    _tree(tmp_path)
    root_path = tmp_path / "dist/manifest.json"
    root = _json(root_path)
    release_path = tmp_path / str(root["release_manifest"]["path"])
    release = _json(release_path)
    root["content_workflow_run_id"] = ""
    release["content_workflow_run_id"] = ""
    release_bytes = _write(release_path, release)
    root["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    _write(root_path, root)
    with pytest.raises(ContractError, match="content identity is invalid"):
        validate_bundle(tmp_path)


def test_bundle_rejects_alias_hash_missing_target_and_byte_drift(tmp_path) -> None:
    _tree(tmp_path)
    root_path = tmp_path / "dist/manifest.json"
    root = _json(root_path)
    root["aliases"]["dist/index.json"] = "bad"
    _write(root_path, root)
    with pytest.raises(ContractError, match="invalid alias hash"):
        validate_bundle(tmp_path)

    _tree(tmp_path := tmp_path / "missing")
    (tmp_path / "dist/index.json").unlink()
    with pytest.raises(ContractError, match="alias target is missing"):
        validate_bundle(tmp_path)

    _tree(tmp_path := tmp_path / "drift")
    alias_path = tmp_path / "dist/index.json"
    changed = _json(alias_path)
    changed["urls"][0]["name"] = "Changed"
    data = _write(alias_path, changed)
    root_path = tmp_path / "dist/manifest.json"
    root = _json(root_path)
    root["aliases"]["dist/index.json"] = prefixed_sha256(data)
    _write(root_path, root)
    with pytest.raises(ContractError, match="not byte-identical"):
        validate_bundle(tmp_path)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"#EXTM3U\r\n", "use LF"),
        (b"#EXTM3U", "end with a newline"),
        (b"not-m3u\n", "start with"),
        (b"#EXTM3U\n#EXTINF:-1,News\n", "EXTINF/URL pairs"),
        (
            b"#EXTM3U\n#EXTVLCOPT:http-referrer=x\nhttps://example.test/live.m3u8\n",
            "unsupported M3U directive",
        ),
        (
            b"#EXTM3U\n#EXTINF:-1,One\nhttps://example.test/live.m3u8\n"
            b"#EXTINF:-1,Two\nhttps://example.test/live.m3u8\n",
            "duplicate playback URLs",
        ),
    ],
)
def test_bundle_m3u_parser_rejects_nonportable_or_duplicate_content(
    tmp_path: Path,
    data: bytes,
    error: str,
) -> None:
    _tree(tmp_path)
    relative = "dist/releases/g00000001/live/stable.m3u"
    _rewrite_release_artifact(tmp_path, relative, data, alias="dist/live/stable.m3u")
    with pytest.raises(ContractError, match=error):
        validate_bundle(tmp_path)


def _tree_with_state(root: Path) -> None:
    context = _context()
    identity = {
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
    }
    report = build_latest_report(
        context,
        status="pending",
        started_at=context.generated_at,
        finished_at=context.generated_at,
        due=False,
        forced=True,
        recovery_due=False,
        sources=[],
        counts={},
        gate={},
        previous_release_head_sha=None,
        candidate_ref=context.candidate_ref,
        content_identity=identity,
    )
    state = {
        "schema_version": "1.0.0",
        "status": "pending",
        "release_kind": "bootstrap",
        "generation": 1,
        "active_release_id": "g00000001",
        "last_publish_at": None,
        "last_success_at": None,
        "content_commit_sha": None,
        "previous_release_head_sha": None,
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
    }
    bundle = build_bundle_files(
        context=context,
        client_artifacts=_client(),
        health=_health(),
        source_count=0,
        supplemental_files={
            "state/release.json": canonical_json_bytes(state),
            "dist/reports/latest.json": canonical_json_bytes(report),
            "dist/reports/latest.md": render_latest_markdown(report),
        },
    )
    materialize_bundle(root, bundle)


@pytest.mark.parametrize(
    ("state_change", "report_change", "error"),
    [
        ({"schema_version": "2.0.0"}, {}, "state schema version"),
        ({"active_release_id": "g00000002"}, {}, "active release differs"),
        ({"workflow_run_id": "other"}, {}, "event identities differ"),
        ({}, {"candidate_ref": "candidate/run-other-attempt-1"}, "candidate ref differs"),
        ({"release_kind": "regular"}, {}, "release kinds differ"),
        (
            {"release_kind": "rollback"},
            {"release_kind": "rollback"},
            "rollback event generation must exceed",
        ),
        (
            {"generation": 2},
            {"generation": 2},
            "new content event and release generations differ",
        ),
        (
            {},
            {"content_identity": {"workflow_run_id": "other", "workflow_run_attempt": 1}},
            "content identity differs",
        ),
    ],
)
def test_state_report_business_identity_is_revalidated(
    tmp_path: Path,
    state_change: dict[str, Any],
    report_change: dict[str, Any],
    error: str,
) -> None:
    _tree_with_state(tmp_path)
    state_path = tmp_path / "state/release.json"
    state = _json(state_path)
    state.update(state_change)
    _write(state_path, state)
    report_path = tmp_path / "dist/reports/latest.json"
    report = _json(report_path)
    report.update(report_change)
    _write(report_path, report)
    (tmp_path / "dist/reports/latest.md").write_bytes(render_latest_markdown(report))
    with pytest.raises(ContractError, match=error):
        validate_bundle(tmp_path)


def test_state_and_reports_are_atomic_and_markdown_matches_json(tmp_path: Path) -> None:
    _tree_with_state(tmp_path)
    (tmp_path / "dist/reports/latest.md").unlink()
    with pytest.raises(ContractError, match="must be present together"):
        validate_bundle(tmp_path)

    _tree_with_state(tmp_path := tmp_path / "markdown")
    (tmp_path / "dist/reports/latest.md").write_bytes(b"\xff")
    with pytest.raises(ContractError, match="must be UTF-8"):
        validate_bundle(tmp_path)

    _tree_with_state(tmp_path := tmp_path / "drift")
    (tmp_path / "dist/reports/latest.md").write_text("# unrelated\n", encoding="utf-8")
    with pytest.raises(ContractError, match="machine-readable JSON"):
        validate_bundle(tmp_path)
