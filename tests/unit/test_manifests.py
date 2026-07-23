from __future__ import annotations

import json
from typing import Any

import pytest

from ds_tvbox.errors import ContractError
from ds_tvbox.manifests import (
    ContentIdentity,
    alias_hashes,
    build_release_manifest,
    build_root_manifest,
    create_release_manifest,
    create_root_manifest,
    prefixed_sha256,
    release_artifact_hashes,
    verify_hash,
)
from ds_tvbox.models import ReleaseKind, RunContext


def _context() -> RunContext:
    return RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="900",
        workflow_run_attempt=2,
        generated_at="2026-07-22T12:00:00Z",
        generation=1,
        release_kind=ReleaseKind.BOOTSTRAP,
        previous_head=None,
        previous_last_success_at=None,
    )


def _release_files() -> dict[str, bytes]:
    prefix = "dist/releases/g00000001"
    return {
        f"{prefix}/index.json": b"index\n",
        f"{prefix}/warehouse.json": b"warehouse\n",
        f"{prefix}/depots/stable.json": b"stable depot\n",
        f"{prefix}/depots/public-unverified.json": b"risk depot\n",
        f"{prefix}/configs/stable.json": b"stable config\n",
        f"{prefix}/configs/z-source.json": b"z source\n",
        f"{prefix}/live/stable.m3u": b"#EXTM3U\n",
        f"{prefix}/health.json": b"health\n",
    }


def _aliases() -> dict[str, bytes]:
    return {
        "dist/index.json": b"index\n",
        "dist/warehouse.json": b"warehouse\n",
        "dist/configs/stable.json": b"stable config\n",
        "dist/live/stable.m3u": b"#EXTM3U\n",
        "dist/health.json": b"health\n",
    }


def _upstream(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "fetch_mode": "direct_url",
        "reviewed_revision": None,
        "resolved_revision": None,
        "resolved_fetch_url": f"https://example.test/{source_id}.json",
        "terms_sha256": {"README.md": "a" * 64},
    }


def test_dual_manifests_are_sorted_closed_and_exclude_themselves() -> None:
    context = _context()
    release = create_release_manifest(
        context=context,
        release_files=_release_files(),
        upstreams=[_upstream("z-source"), _upstream("a-source")],
        source_count=2,
        vod_site_count=1,
        live_channel_count=0,
    )
    release_document = json.loads(release.data)

    assert list(release_document["artifacts"]) == sorted(release_document["artifacts"])
    assert "dist/releases/g00000001/manifest.json" not in release_document["artifacts"]
    assert [item["source_id"] for item in release_document["upstreams"]] == [
        "a-source",
        "z-source",
    ]
    assert release.sha256 == prefixed_sha256(release.data)

    root = create_root_manifest(
        context=context,
        release_manifest_bytes=release.data,
        alias_files=_aliases(),
    )
    root_document = json.loads(root.data)
    assert root_document["release_manifest"] == {
        "path": "dist/releases/g00000001/manifest.json",
        "sha256": release.sha256,
    }
    assert list(root_document["aliases"]) == sorted(root_document["aliases"])
    assert "dist/manifest.json" not in root_document["aliases"]
    assert root_document["content_workflow_run_id"] == "900"
    assert root_document["content_workflow_run_attempt"] == 2


def test_explicit_old_content_identity_is_preserved_in_both_manifests() -> None:
    context = _context()
    old_identity = ContentIdentity("old-run", 7)
    release = create_release_manifest(
        context=context,
        release_files=_release_files(),
        upstreams=[],
        source_count=0,
        content_identity=old_identity,
    )
    root = create_root_manifest(
        context=context,
        release_manifest_bytes=release.data,
        alias_files=_aliases(),
        content_identity=old_identity,
    )
    for document in (json.loads(release.data), json.loads(root.data)):
        assert document["content_workflow_run_id"] == "old-run"
        assert document["content_workflow_run_attempt"] == 7


def test_release_hash_set_rejects_missing_cross_release_and_self_paths() -> None:
    files = _release_files()
    files.pop("dist/releases/g00000001/health.json")
    with pytest.raises(ContractError, match="missing"):
        release_artifact_hashes("g00000001", files)

    files = _release_files()
    files["dist/releases/g00000002/index.json"] = b"other"
    with pytest.raises(ContractError, match="outside"):
        release_artifact_hashes("g00000001", files)

    files = _release_files()
    files["dist/releases/g00000001/manifest.json"] = b"self"
    with pytest.raises(ContractError, match="exclude itself"):
        release_artifact_hashes("g00000001", files)


def test_alias_hash_set_is_exact_and_excludes_root_manifest() -> None:
    aliases = _aliases()
    assert tuple(alias_hashes(aliases)) == tuple(sorted(aliases))

    aliases["dist/manifest.json"] = b"self"
    with pytest.raises(ContractError, match="invalid alias set"):
        alias_hashes(aliases)


def test_manifest_rejects_credentialed_upstream_before_serialization() -> None:
    upstream = _upstream("bad-source")
    upstream["resolved_fetch_url"] = "https://example.test/config.json?token=secret"
    with pytest.raises(ContractError, match="credentials"):
        create_release_manifest(
            context=_context(),
            release_files=_release_files(),
            upstreams=[upstream],
            source_count=1,
        )


@pytest.mark.parametrize(
    ("run_id", "attempt"),
    [("", 1), ("run", 0)],
)
def test_content_identity_must_be_nonempty_and_positive(run_id: str, attempt: int) -> None:
    with pytest.raises(ContractError, match="invalid content workflow identity"):
        ContentIdentity(run_id, attempt)


def test_release_artifact_paths_and_bytes_are_strict() -> None:
    with pytest.raises(ContractError, match="invalid release id"):
        release_artifact_hashes("release-1", _release_files())

    files = _release_files()
    files[""] = b"bad"
    with pytest.raises(ContractError, match="invalid repository path"):
        release_artifact_hashes("g00000001", files)

    files = _release_files()
    files["dist/releases/g00000001/index.json"] = "not-bytes"  # type: ignore[assignment]
    with pytest.raises(ContractError, match="is not bytes"):
        release_artifact_hashes("g00000001", files)

    aliases = _aliases()
    aliases["dist/index.json"] = "not-bytes"  # type: ignore[assignment]
    with pytest.raises(ContractError, match="is not bytes"):
        alias_hashes(aliases)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value.pop("terms_sha256"), "is missing"),
        (lambda value: value.update(source_id="Bad_ID"), "invalid upstream source_id"),
        (lambda value: value.update(fetch_mode="unsupported"), "invalid upstream fetch_mode"),
        (
            lambda value: value.update(reviewed_revision="a" * 40),
            "must not claim revisions",
        ),
        (
            lambda value: value.update(
                fetch_mode="github_tracked_file",
                reviewed_revision="short",
                resolved_revision="b" * 40,
            ),
            "revisions are invalid",
        ),
        (lambda value: value.update(resolved_fetch_url=1), "resolved_fetch_url is invalid"),
        (lambda value: value.update(resolved_fetch_url="file:///tmp/x"), "not public HTTP"),
        (lambda value: value.update(terms_sha256=[]), "must be an object"),
        (lambda value: value.update(terms_sha256={"": "a" * 64}), "invalid terms hash"),
        (lambda value: value.update(terms_sha256={"README": "short"}), "invalid terms hash"),
    ],
)
def test_upstream_records_are_fail_closed(mutate: Any, error: str) -> None:
    upstream = _upstream("source")
    mutate(upstream)
    with pytest.raises(ContractError, match=error):
        create_release_manifest(
            context=_context(),
            release_files=_release_files(),
            upstreams=[upstream],
            source_count=1,
        )


def test_upstream_ids_are_unique_and_hash_prefix_is_normalized() -> None:
    first = _upstream("source")
    second = _upstream("source")
    with pytest.raises(ContractError, match="duplicate upstream"):
        create_release_manifest(
            context=_context(),
            release_files=_release_files(),
            upstreams=[first, second],
            source_count=2,
        )

    first["terms_sha256"] = {"LICENSE": "sha256:" + "b" * 64}
    manifest = build_release_manifest(
        context=_context(),
        release_files=_release_files(),
        upstreams=[first],
    )
    assert manifest["upstreams"][0]["terms_sha256"] == {"LICENSE": "b" * 64}


def test_manifest_counts_and_root_inputs_are_validated() -> None:
    with pytest.raises(ContractError, match="counts cannot be negative"):
        build_release_manifest(
            context=_context(),
            release_files=_release_files(),
            vod_site_count=-1,
        )
    with pytest.raises(ContractError, match="source_count cannot be negative"):
        build_release_manifest(
            context=_context(),
            release_files=_release_files(),
            source_count=-1,
        )
    with pytest.raises(ContractError, match="must equal"):
        build_release_manifest(
            context=_context(),
            release_files=_release_files(),
            upstreams=[_upstream("source")],
            source_count=0,
        )
    with pytest.raises(ContractError, match="exact serialized bytes"):
        build_root_manifest(
            context=_context(),
            release_manifest_bytes="json",  # type: ignore[arg-type]
            alias_files=_aliases(),
        )


def test_verify_hash_accepts_only_exact_prefixed_digest() -> None:
    data = b"payload"
    verify_hash(prefixed_sha256(data), data, label="fixture")
    for digest in ("0" * 64, "sha256:" + "0" * 64):
        with pytest.raises(ContractError, match="hash mismatch"):
            verify_hash(digest, data, label="fixture")
