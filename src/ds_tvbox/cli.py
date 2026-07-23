"""Command-line boundary for validation, collection, and publication jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from ds_tvbox.artifact import validate_publish_artifact
from ds_tvbox.errors import ContractError, DsTvboxError, PublishError, SecurityError
from ds_tvbox.gitops import Git, validate_ref, validate_sha
from ds_tvbox.publisher import Publisher
from ds_tvbox.raw import RawVerifier
from ds_tvbox.registry import load_registry, load_yaml_strict
from ds_tvbox.schedule import DueDecision, evaluate_due
from ds_tvbox.security import normalize_client_url_offline, validate_registry_host
from ds_tvbox.validation import validate_schema

Command = Callable[[argparse.Namespace], int]


def _bool_arg(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = child
    return value


def _decode_object(data: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as error:
        raise ContractError(f"invalid JSON in {label}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} must contain a JSON object")
    return value


def _project_paths(repository: Path) -> tuple[Path, Path, Path]:
    root = repository.resolve()
    return root, root / "sources/registry.yaml", root / "schemas"


def _load_mapping(path: Path) -> Mapping[str, Any]:
    try:
        value = load_yaml_strict(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {path}") from error
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must contain a mapping")
    return value


def _compatibility(repository: Path) -> Mapping[str, Any]:
    value = _load_mapping(repository / "config/compatibility.yaml")
    expected = {"version", "owner", "repository", "generated_ref", "clients"}
    if set(value) != expected:
        raise ContractError("compatibility config fields are invalid")
    if value["version"] != 1 or value["generated_ref"] != "generated":
        raise ContractError("compatibility config version/ref is unsupported")
    owner = value["owner"]
    repository_name = value["repository"]
    clients = value["clients"]
    if not isinstance(owner, str) or not owner or "/" in owner:
        raise ContractError("compatibility owner is invalid")
    if not isinstance(repository_name, str) or not repository_name or "/" in repository_name:
        raise ContractError("compatibility repository is invalid")
    if not isinstance(clients, list) or not clients:
        raise ContractError("compatibility client baseline is empty")
    return value


def _policy(repository: Path) -> Mapping[str, Any]:
    value = _load_mapping(repository / "config/policy.yaml")
    if value.get("version") != 1:
        raise ContractError("policy version is unsupported")
    refresh = value.get("refresh")
    if not isinstance(refresh, Mapping) or refresh.get("due_hours") != 240:
        raise ContractError("policy refresh gate must be exactly 240 hours")
    return value


def _validate_denylist_matchers(value: Mapping[str, Any]) -> None:
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ContractError("denylist entries must be an array")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ContractError("denylist entry must be an object")
        entry_id = str(entry.get("id", "<unknown>"))
        for raw_host in entry.get("hosts", []):
            if not isinstance(raw_host, str):
                raise ContractError(f"denylist {entry_id} host must be a string")
            if validate_registry_host(raw_host) != raw_host:
                raise ContractError(f"denylist {entry_id} host must be canonical lowercase DNS")
        for raw_url in entry.get("urls", []):
            if not isinstance(raw_url, str):
                raise ContractError(f"denylist {entry_id} URL must be a string")
            try:
                normalized = normalize_client_url_offline(raw_url)
            except SecurityError as error:
                raise ContractError(
                    f"denylist {entry_id} URL is not credential-free public HTTPS"
                ) from error
            if normalized.scheme != "https" or normalized.value != raw_url:
                raise ContractError(
                    f"denylist {entry_id} URL must be canonical credential-free HTTPS"
                )


def _cmd_validate_static(args: argparse.Namespace) -> int:
    root, registry_path, schema_root = _project_paths(args.repository)
    load_registry(registry_path, schema_path=schema_root / "source-registry.schema.json")
    _policy(root)
    _compatibility(root)
    denylist = _load_mapping(root / "sources/denylist.yaml")
    validate_schema(denylist, schema_root / "denylist.schema.json")
    _validate_denylist_matchers(denylist)
    for path in sorted(schema_root.glob("*.schema.json")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ContractError(f"invalid schema JSON: {path}") from error
        Draft202012Validator.check_schema(schema)
    print("static validation passed")
    return 0


def _exact_remote_state(git: Git, head: str | None) -> Mapping[str, Any] | None:
    if head is None:
        return None
    git.fetch_sha(head)
    result = git.run("show", f"{head}:state/release.json", check=False)
    if result.returncode != 0:
        raise ContractError("generated ref exists without state/release.json")
    return _decode_object(result.stdout, "generated state/release.json")


def _decision_payload(decision: DueDecision, head: str | None) -> dict[str, str]:
    return {
        "should_refresh": str(decision.should_refresh).lower(),
        "due": str(decision.due).lower(),
        "forced": str(decision.forced).lower(),
        "recovery_due": str(decision.recovery_due).lower(),
        "bootstrap_required": str(decision.bootstrap_required).lower(),
        "reason": decision.reason,
        "previous_head": head or "",
        "last_success_at": decision.last_success_at.isoformat() if decision.last_success_at else "",
        "next_due_at": decision.next_due_at.isoformat() if decision.next_due_at else "",
    }


def _write_github_output(path: Path | None, values: Mapping[str, str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ContractError(f"GitHub output {key} contains a newline")
            stream.write(f"{key}={value}\n")


def _cmd_due_check(args: argparse.Namespace) -> int:
    root = args.repository.resolve()
    git = Git(root)
    head = git.remote_head()
    state = _exact_remote_state(git, head)
    decision = evaluate_due(
        now=datetime.now(UTC),
        state=state,
        generated_ref_exists=head is not None,
        force=args.force,
        bootstrap=args.bootstrap,
    )
    payload = _decision_payload(decision, head)
    _write_github_output(args.github_output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_verify_artifact(args: argparse.Namespace) -> int:
    root = args.repository.resolve()
    artifact = validate_publish_artifact(args.artifact, root / "schemas")
    print(
        json.dumps(
            {
                "generation": artifact.generation,
                "release_id": artifact.release_id,
                "release_kind": artifact.release_kind.value,
            },
            sort_keys=True,
        )
    )
    return 0


def _raw_verifier(repository: Path) -> RawVerifier:
    compatibility = _compatibility(repository)
    return RawVerifier(
        str(compatibility["owner"]),
        str(compatibility["repository"]),
        repository / "schemas",
    )


def _cmd_publish(args: argparse.Namespace) -> int:
    root = args.repository.resolve()
    publisher = Publisher(
        repository=root,
        schemas_dir=root / "schemas",
        raw_verifier=_raw_verifier(root),
    )
    sha = publisher.publish(args.artifact)
    print(json.dumps({"published_sha": sha}, sort_keys=True))
    return 0


def _cmd_verify_release(args: argparse.Namespace) -> int:
    verifier = _raw_verifier(args.repository.resolve())
    verifier.poll_revision(
        args.revision,
        timeout_seconds=args.timeout,
        interval_seconds=args.interval,
        expected_status=args.expected_status,
    )
    print(json.dumps({"revision": args.revision, "status": "verified"}, sort_keys=True))
    return 0


def _cmd_preflight_ref(args: argparse.Namespace) -> int:
    root = args.repository.resolve()
    git = Git(root)
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    ref = validate_ref(f"candidate/run-preflight-{run_id}-attempt-{attempt}")
    if git.remote_head(ref) is not None:
        raise PublishError(f"preflight ref already exists: {ref}")
    base_sha = validate_sha(git.run("rev-parse", "HEAD").stdout.strip())
    fast_forward_sha = git.create_empty_child(
        base_sha, "chore: 验证 TVBox 预检分支快进权限"
    )
    divergent_sha = git.create_empty_child(
        base_sha, "chore: 验证 TVBox 预检分支禁止强推"
    )
    git.push_if_remote_equals(base_sha, ref, None)
    git.push_if_remote_equals(fast_forward_sha, ref, base_sha)

    forced = git.run(
        "push",
        f"--force-with-lease=refs/heads/{ref}:{fast_forward_sha}",
        git.remote,
        f"{divergent_sha}:refs/heads/{ref}",
        check=False,
    )
    observed = git.remote_head(ref)
    rejection = f"{forced.stdout}\n{forced.stderr}".casefold()
    if forced.returncode == 0:
        # A misconfigured ruleset can allow the force.  Remove only the exact
        # commit created by this preflight; a concurrent replacement is untouchable.
        if observed == divergent_sha:
            git.delete_candidate(ref, divergent_sha)
        raise PublishError("preflight ruleset allowed a non-fast-forward force push")
    if "gh013" not in rejection or (
        "force" not in rejection and "non-fast-forward" not in rejection
    ):
        raise PublishError("preflight did not observe an explicit GitHub ruleset rejection")
    if observed != fast_forward_sha:
        raise PublishError("preflight rejected force push changed the remote ref")
    git.delete_candidate(ref, fast_forward_sha)
    print(json.dumps({"ref": ref, "status": "verified"}, sort_keys=True))
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    # Imported lazily so the privileged publish job never imports collector code.
    from ds_tvbox.pipeline import collect_publish_artifact

    collect_publish_artifact(
        repository=args.repository.resolve(),
        output=args.output.resolve(),
        force=args.force,
        bootstrap=args.bootstrap,
    )
    print(json.dumps({"artifact": str(args.output.resolve())}, sort_keys=True))
    return 0


def _add_repository(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", type=Path, default=Path.cwd())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ds-tvbox")
    subparsers = parser.add_subparsers(dest="command", required=True)

    static = subparsers.add_parser("validate-static")
    _add_repository(static)
    static.set_defaults(handler=_cmd_validate_static)

    due = subparsers.add_parser("due-check")
    _add_repository(due)
    due.add_argument("--force", type=_bool_arg, default=False)
    due.add_argument("--bootstrap", type=_bool_arg, default=False)
    due.add_argument("--github-output", type=Path)
    due.set_defaults(handler=_cmd_due_check)

    collect = subparsers.add_parser("collect")
    _add_repository(collect)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--force", type=_bool_arg, default=False)
    collect.add_argument("--bootstrap", type=_bool_arg, default=False)
    collect.set_defaults(handler=_cmd_collect)

    verify_artifact = subparsers.add_parser("verify-artifact")
    _add_repository(verify_artifact)
    verify_artifact.add_argument("artifact", type=Path)
    verify_artifact.set_defaults(handler=_cmd_verify_artifact)

    publish = subparsers.add_parser("publish")
    _add_repository(publish)
    publish.add_argument("--artifact", type=Path, required=True)
    publish.set_defaults(handler=_cmd_publish)

    verify_release = subparsers.add_parser("verify-release")
    _add_repository(verify_release)
    verify_release.add_argument("revision")
    verify_release.add_argument(
        "--expected-status", choices=("pending", "success"), default="success"
    )
    verify_release.add_argument("--timeout", type=int, default=120)
    verify_release.add_argument("--interval", type=int, default=5)
    verify_release.set_defaults(handler=_cmd_verify_release)

    preflight = subparsers.add_parser("preflight-ref")
    _add_repository(preflight)
    preflight.set_defaults(handler=_cmd_preflight_ref)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Command = args.handler
    try:
        return handler(args)
    except (DsTvboxError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
