from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
}


def _load(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(workflow: Mapping[Any, Any]) -> Mapping[str, Any]:
    # PyYAML follows YAML 1.1 and may decode the unquoted key `on` as True.
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, Mapping)
    return value


def _uses(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        use = value.get("uses")
        if isinstance(use, str):
            yield use
        for child in value.values():
            yield from _uses(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _uses(child)


def _run_text(job: Mapping[str, Any]) -> str:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return "\n".join(
        str(step["run"])
        for step in steps
        if isinstance(step, Mapping) and "run" in step
    )


def test_all_workflows_are_least_privilege_and_actions_are_sha_pinned() -> None:
    paths = sorted(WORKFLOW_DIR.glob("*.yml"))
    assert {path.name for path in paths} == {"preflight.yml", "refresh.yml", "validate.yml"}

    for path in paths:
        workflow = _load(path.name)
        assert workflow["permissions"] == {}
        assert "pull_request_target" not in _triggers(workflow)

        serialized = path.read_text(encoding="utf-8")
        assert "${{ secrets." not in serialized
        assert "PERSONAL_ACCESS_TOKEN" not in serialized
        assert "GITHUB_PAT" not in serialized
        assert "GH_PAT" not in serialized

        uses = list(_uses(workflow))
        assert uses
        for use in uses:
            action, separator, revision = use.partition("@")
            assert separator
            assert action in PINNED_ACTIONS
            assert revision == PINNED_ACTIONS[action]
            assert re.fullmatch(r"[0-9a-f]{40}", revision)


def test_every_job_installs_only_hash_locked_third_party_dependencies() -> None:
    for name in ("validate.yml", "refresh.yml", "preflight.yml"):
        workflow = _load(name)
        jobs = workflow["jobs"]
        assert isinstance(jobs, Mapping)
        for job in jobs.values():
            assert isinstance(job, Mapping)
            run_text = _run_text(job)
            assert "python -m pip install --require-hashes -r requirements.lock" in run_text
            assert "python -m pip install --no-deps --no-build-isolation -e ." in run_text


def test_validate_workflow_is_read_only_and_runs_the_full_local_gate() -> None:
    workflow = _load("validate.yml")
    triggers = _triggers(workflow)
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}

    jobs = workflow["jobs"]
    assert set(jobs) == {"validate"}
    validate = jobs["validate"]
    assert validate["permissions"] == {"contents": "read"}
    run_text = _run_text(validate)
    for command in (
        "ds-tvbox validate-static --repository .",
        "pytest --cov=ds_tvbox --cov-report=term-missing",
        "ruff check .",
        "mypy src",
    ):
        assert command in run_text


def test_refresh_trigger_due_gate_concurrency_and_permissions() -> None:
    workflow = _load("refresh.yml")
    triggers = _triggers(workflow)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 19 * * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    for name in ("force", "bootstrap"):
        assert inputs[name]["type"] == "boolean"
        assert inputs[name]["default"] is False
        assert inputs[name]["required"] is False

    assert workflow["concurrency"] == {
        "group": "refresh-tvbox-sources",
        "cancel-in-progress": False,
    }

    jobs = workflow["jobs"]
    assert set(jobs) == {"collect-and-validate", "publish"}
    collect = jobs["collect-and-validate"]
    publish = jobs["publish"]
    assert collect["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"contents": "write"}
    assert collect["timeout-minutes"] == 90
    assert publish["timeout-minutes"] == 90
    assert "should_refresh" in collect["outputs"]
    assert "should_refresh == 'true'" in publish["if"]

    for job_name, job in jobs.items():
        if job_name != "publish":
            assert job["permissions"].get("contents") != "write"


def test_refresh_cli_and_attempt_isolated_artifact_contract() -> None:
    workflow = _load("refresh.yml")
    collect = workflow["jobs"]["collect-and-validate"]
    publish = workflow["jobs"]["publish"]
    collect_text = _run_text(collect)
    publish_text = _run_text(publish)

    assert "ds-tvbox due-check --repository ." in collect_text
    assert "--github-output \"$GITHUB_OUTPUT\"" in collect_text
    assert "ds-tvbox collect --repository ." in collect_text
    assert "--output action-artifact" in collect_text
    assert "ds-tvbox verify-artifact action-artifact/publish" in collect_text
    assert "ds-tvbox verify-artifact action-artifact/publish" in publish_text
    assert (
        "ds-tvbox publish --repository . --artifact action-artifact/publish" in publish_text
    )

    collect_steps = collect["steps"]
    upload = next(step for step in collect_steps if str(step.get("uses", "")).startswith(
        "actions/upload-artifact@"
    ))
    assert upload["with"]["path"] == "action-artifact"
    assert upload["with"]["retention-days"] == 30
    artifact_name = upload["with"]["name"]
    assert "${{ github.run_id }}" in artifact_name
    assert "${{ github.run_attempt }}" in artifact_name
    assert "always()" in upload["if"]
    assert "should_refresh == 'true'" in upload["if"]

    publish_steps = publish["steps"]
    download = next(
        step
        for step in publish_steps
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert download["with"]["name"] == artifact_name
    assert download["with"]["path"] == "action-artifact"


def test_preflight_is_manual_and_uses_the_only_write_job_name() -> None:
    workflow = _load("preflight.yml")
    assert set(_triggers(workflow)) == {"workflow_dispatch"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert set(workflow["jobs"]) == {"publish"}
    publish = workflow["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write"}
    assert "ds-tvbox preflight-ref --repository ." in _run_text(publish)


def test_requirements_lock_is_complete_pinned_and_hashed() -> None:
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lock
    assert "git+" not in lock
    assert " -e " not in lock
    assert not re.search(r"^https?://", lock, flags=re.MULTILINE)

    pinned = set(re.findall(r"^([a-z0-9][a-z0-9-]*)==[^\\\s]+", lock, re.MULTILINE))
    assert {
        "defusedxml",
        "json5",
        "jsonschema",
        "pyyaml",
        "mypy",
        "pytest",
        "pytest-cov",
        "pytest-timeout",
        "ruff",
        "types-pyyaml",
        "setuptools",
        "wheel",
    } <= pinned

    blocks = re.split(r"(?m)(?=^[a-z0-9][a-z0-9-]*==)", lock)
    requirement_blocks = [block for block in blocks if re.match(r"^[a-z0-9-]+==", block)]
    assert requirement_blocks
    assert all(
        "--hash=sha256:" in block.split("\n    # via", maxsplit=1)[0]
        for block in requirement_blocks
    )
