from pathlib import Path

import pytest

from ds_tvbox.errors import ContractError
from ds_tvbox.validation import validate_schema

SCHEMA = Path(__file__).resolve().parents[2] / "schemas/candidates.schema.json"


def _document() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "workflow_run_id": "42",
        "workflow_run_attempt": 2,
        "catalogs": [],
        "candidates": [
            {
                "candidate_id": "candidate:catalog-source:0123456789abcdef",
                "kind": "vod_site",
                "normalized_target_hash": "a" * 64,
                "technical_status": "healthy",
                "rights_status": "unknown",
                "publication_status": "withheld",
                "evidence_locations": [
                    "https://github.com/example/repo@" + "b" * 40 + ":a.json#/sites/0"
                ],
                "failure_reason": None,
                "secondary_reasons": [],
                "url": "https://media.example.test/api.php/provide/vod",
            }
        ],
    }


def test_candidate_schema_locks_unknown_to_report_only() -> None:
    validate_schema(_document(), SCHEMA)


def test_candidate_schema_rejects_inherited_public_rights() -> None:
    document = _document()
    candidates = document["candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    candidate["rights_status"] = "public_unverified"
    with pytest.raises(ContractError):
        validate_schema(document, SCHEMA)
