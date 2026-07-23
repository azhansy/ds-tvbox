"""Deterministic, report-only repository catalog discovery.

Catalogs are an untrusted discovery surface.  Every discovered target is
therefore forced to ``unknown/withheld`` here and is never converted into a
``VodProbeResult`` or ``LiveProbeResult`` consumed by the generator.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from .errors import ContractError, SecurityError
from .models import FailureReason, PublicationStatus, RightsStatus, SourceSpec, TechnicalStatus
from .parsers import parse_json5_data, parse_json_data, parse_m3u, parse_txt_live
from .security import normalize_url, validate_declared_headers
from .upstream import (
    Fetcher,
    UpstreamFailure,
    UpstreamSnapshot,
    fetch_github_file,
    fetch_github_tree,
)

CatalogCandidateKind = Literal["vod_site", "live_url", "nested_config"]
_EXECUTABLE = re.compile(r"(?i)\.(?:jar|js|py|dex|so)(?:$|[?#])")


@dataclass(frozen=True)
class CatalogCandidate:
    candidate_id: str
    kind: CatalogCandidateKind
    normalized_target_hash: str
    technical_status: TechnicalStatus
    rights_status: RightsStatus
    publication_status: PublicationStatus
    evidence_locations: tuple[str, ...]
    failure_reason: FailureReason | None
    secondary_reasons: tuple[FailureReason, ...] = ()
    public_url: str | None = None

    def as_report(self) -> dict[str, object]:
        value: dict[str, object] = {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "normalized_target_hash": self.normalized_target_hash,
            "technical_status": self.technical_status.value,
            "rights_status": self.rights_status.value,
            "publication_status": self.publication_status.value,
            "evidence_locations": list(self.evidence_locations),
            "failure_reason": (
                self.failure_reason.value if self.failure_reason is not None else None
            ),
            "secondary_reasons": [reason.value for reason in self.secondary_reasons],
        }
        if self.public_url is not None:
            value["url"] = self.public_url
        return value


@dataclass(frozen=True)
class CatalogScanResult:
    source_id: str
    reviewed_revision: str
    resolved_revision: str
    technical_status: TechnicalStatus
    publication_status: PublicationStatus
    inconclusive: bool
    files_scanned: int
    candidates: tuple[CatalogCandidate, ...]
    failure_reason: FailureReason | None = None
    secondary_reasons: tuple[FailureReason, ...] = ()

    def as_report(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "reviewed_revision": self.reviewed_revision,
            "resolved_revision": self.resolved_revision,
            "technical_status": self.technical_status.value,
            "publication_status": self.publication_status.value,
            "inconclusive": self.inconclusive,
            "files_scanned": self.files_scanned,
            "failure_reason": (
                self.failure_reason.value if self.failure_reason is not None else None
            ),
            "secondary_reasons": [reason.value for reason in self.secondary_reasons],
            "candidates": [candidate.as_report() for candidate in self.candidates],
        }


@dataclass
class _CandidateAccumulator:
    candidate_id: str
    kind: CatalogCandidateKind
    normalized_target_hash: str
    identity: str
    technical_status: TechnicalStatus
    failure_reason: FailureReason | None
    public_url: str | None
    secondary_reasons: set[FailureReason] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)


class _CatalogLimit(Exception):
    pass


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate the deliberately small registry glob language to POSIX regex."""

    output = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    output.append("(?:[^/]+/)*")
                    index += 1
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
        elif character == "?":
            output.append("[^/]")
        else:
            output.append(re.escape(character))
        index += 1
    output.append("$")
    return re.compile("".join(output))


def _pointer(root: object, pointer: str) -> list[object]:
    current = root
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return []
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return []
            current = current[index]
        else:
            return []
    if not isinstance(current, list):
        raise ContractError(f"catalog selector does not locate an array: {pointer}")
    return current


def _security_reason(exc: SecurityError) -> FailureReason:
    message = str(exc).casefold()
    if "credential" in message or "query key" in message:
        return FailureReason.CREDENTIAL_QUERY_REJECTED
    if "header" in message:
        return FailureReason.CREDENTIAL_HEADER_REJECTED
    if "scheme" in message:
        return FailureReason.DANGEROUS_SCHEME_REJECTED
    if "http" in message:
        return FailureReason.CLIENT_HTTP_DISALLOWED
    return FailureReason.PRIVATE_ADDRESS_REJECTED


def _evidence(source: SourceSpec, revision: str, path: str, pointer: str) -> str:
    assert source.fetch.repository_url is not None
    return f"{source.fetch.repository_url}@{revision}:{path}#{pointer}"


def _hidden_identity(raw_target: str) -> str:
    return "rejected:" + hashlib.sha256(raw_target.encode("utf-8")).hexdigest()


def _candidate_id(source_id: str, kind: CatalogCandidateKind, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{identity}".encode()).hexdigest()[:16]
    return f"candidate:{source_id}:{digest}"


def _same_repository_path(
    source: SourceSpec,
    revision: str,
    target: str,
) -> str | None:
    assert source.fetch.repository_url is not None
    repository_parts = tuple(
        part for part in urlsplit(source.fetch.repository_url).path.split("/") if part
    )
    parsed = urlsplit(target)
    try:
        host = parsed.hostname.casefold() if parsed.hostname is not None else ""
    except ValueError:
        return None
    decoded = unquote(parsed.path)
    parts = tuple(part for part in decoded.split("/") if part)
    relative: tuple[str, ...] | None = None
    if host == "raw.githubusercontent.com" and len(parts) >= 4:
        if parts[:2] == repository_parts and parts[2] == revision:
            relative = parts[3:]
    elif (
        host == "github.com"
        and len(parts) >= 5
        and parts[:2] == repository_parts
        and parts[2] in {"blob", "raw"}
        and parts[3] == revision
    ):
        relative = parts[4:]
    if relative is None or not relative or any(part in {".", ".."} for part in relative):
        return None
    return "/".join(relative)


def _parser_for_path(path: str, mappings: Sequence[Mapping[str, Any]]) -> str:
    matches = [
        str(mapping["parser"])
        for mapping in mappings
        if _glob_regex(str(mapping["glob"])).fullmatch(path)
    ]
    if len(matches) != 1:
        raise ContractError(f"catalog file must match exactly one parser glob: {path}")
    return matches[0]


class _Scanner:
    def __init__(
        self,
        source: SourceSpec,
        snapshot: UpstreamSnapshot,
        client: Fetcher,
    ) -> None:
        if source.catalog is None or snapshot.github is None:
            raise ContractError("catalog scan requires a resolved repository snapshot")
        self.source = source
        self.snapshot = snapshot
        self.github = snapshot.github
        self.client = client
        self.contract = source.catalog
        self.max_depth = int(self.contract["max_depth"])
        self.max_files = int(self.contract["max_files"])
        self.max_candidates = int(self.contract["max_candidates"])
        self.max_live_urls = int(self.contract["max_live_urls"])
        self.downstream_hosts = frozenset(
            str(value) for value in self.contract["allowed_downstream_hosts"]
        )
        raw_mappings = self.contract["parsers_by_glob"]
        assert isinstance(raw_mappings, list)
        self.mappings = tuple(mapping for mapping in raw_mappings if isinstance(mapping, Mapping))
        self.candidates: dict[str, _CandidateAccumulator] = {}
        self.live_identities: set[str] = set()
        self.files_scanned = 0
        self.depth_exceeded = False

    def _add(
        self,
        *,
        kind: CatalogCandidateKind,
        identity: str,
        public_url: str | None,
        evidence: str,
        failure_reason: FailureReason | None = None,
    ) -> None:
        candidate_id = _candidate_id(self.source.id, kind, identity)
        target_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        existing = self.candidates.get(candidate_id)
        if existing is not None:
            if existing.kind != kind or existing.identity != identity:
                raise ContractError("catalog candidate hash collision")
            existing.evidence.add(evidence)
            if failure_reason is not None and existing.failure_reason != failure_reason:
                existing.secondary_reasons.add(failure_reason)
            return
        if len(self.candidates) + 1 > self.max_candidates:
            raise _CatalogLimit
        if kind == "live_url" and identity not in self.live_identities:
            if len(self.live_identities) + 1 > self.max_live_urls:
                raise _CatalogLimit
            self.live_identities.add(identity)
        self.candidates[candidate_id] = _CandidateAccumulator(
            candidate_id=candidate_id,
            kind=kind,
            normalized_target_hash=target_hash,
            identity=identity,
            technical_status=(
                TechnicalStatus.PARTIAL
                if failure_reason is FailureReason.UNSUPPORTED_SPIDER
                else TechnicalStatus.DEAD
                if failure_reason is not None
                else TechnicalStatus.UNKNOWN
            ),
            failure_reason=failure_reason,
            public_url=public_url,
            evidence={evidence},
        )

    def _normalize_target(
        self,
        raw_target: str,
    ) -> tuple[str, str | None, FailureReason | None]:
        if _EXECUTABLE.search(raw_target):
            return _hidden_identity(raw_target), None, FailureReason.UNSUPPORTED_SPIDER
        try:
            normalized = normalize_url(
                raw_target,
                allowed_hosts=self.downstream_hosts,
                client_visible=True,
            )
        except SecurityError as exc:
            return _hidden_identity(raw_target), None, _security_reason(exc)
        return normalized.value, normalized.value, None

    def _add_url_candidate(
        self,
        *,
        kind: CatalogCandidateKind,
        raw_target: str,
        evidence: str,
        site_type: int | None = None,
        header: object = None,
        forced_reason: FailureReason | None = None,
    ) -> None:
        identity, public_url, reason = self._normalize_target(raw_target)
        reason = reason or forced_reason
        if site_type is not None:
            identity = f"{site_type}\0{identity}"
        if header is not None and reason is None:
            try:
                validate_declared_headers(header)  # type: ignore[arg-type]
            except (SecurityError, TypeError) as exc:
                reason = (
                    _security_reason(exc)
                    if isinstance(exc, SecurityError)
                    else FailureReason.INVALID_HEADER_SYNTAX
                )
                public_url = None
        self._add(
            kind=kind,
            identity=identity,
            public_url=public_url,
            evidence=evidence,
            failure_reason=reason,
        )

    def _nested(
        self,
        raw_target: str,
        evidence: str,
        *,
        depth: int,
        queue: deque[tuple[str, int]],
    ) -> None:
        try:
            safe_repository_target = normalize_url(
                raw_target,
                allowed_hosts={"raw.githubusercontent.com", "github.com"},
                client_visible=True,
            ).value
        except SecurityError:
            safe_repository_target = None
        same_path = (
            _same_repository_path(
                self.source,
                self.github.resolved_revision,
                safe_repository_target,
            )
            if safe_repository_target is not None
            else None
        )
        if same_path is not None:
            assert safe_repository_target is not None
            identity = safe_repository_target
            self._add(
                kind="nested_config",
                identity=identity,
                public_url=safe_repository_target,
                evidence=evidence,
            )
            if depth + 1 > self.max_depth:
                self.depth_exceeded = True
                return
            try:
                _parser_for_path(same_path, self.mappings)
            except ContractError:
                return
            queue.append((same_path, depth + 1))
            return
        self._add_url_candidate(
            kind="nested_config",
            raw_target=raw_target,
            evidence=evidence,
        )

    def _process_json(
        self,
        body: bytes,
        *,
        parser: str,
        base_url: str,
        path: str,
        depth: int,
        queue: deque[tuple[str, int]],
    ) -> None:
        root = parse_json5_data(body) if parser == "tvbox_json5" else parse_json_data(body)
        selectors = self.contract["selectors"]
        assert isinstance(selectors, Mapping)
        revision = self.github.resolved_revision

        for pointer in selectors["sites_arrays"]:
            for index, item in enumerate(_pointer(root, str(pointer))):
                location = _evidence(self.source, revision, path, f"{pointer}/{index}")
                if not isinstance(item, Mapping):
                    self._add(
                        kind="vod_site",
                        identity=_hidden_identity(repr(item)),
                        public_url=None,
                        evidence=location,
                        failure_reason=FailureReason.SCHEMA_INCOMPATIBLE,
                    )
                    continue
                site_type = item.get("type")
                api = item.get("api")
                if (
                    type(site_type) is not int
                    or site_type not in {0, 1, 3, 4}
                    or not isinstance(api, str)
                ):
                    self._add(
                        kind="vod_site",
                        identity=_hidden_identity(repr((site_type, api))),
                        public_url=None,
                        evidence=location,
                        failure_reason=FailureReason.SCHEMA_INCOMPATIBLE,
                    )
                    continue
                from urllib.parse import urljoin

                self._add_url_candidate(
                    kind="vod_site",
                    raw_target=urljoin(base_url, api),
                    evidence=location,
                    site_type=site_type,
                    header=item.get("header"),
                    forced_reason=(
                        FailureReason.UNSUPPORTED_SPIDER
                        if site_type == 3
                        or any(
                            _EXECUTABLE.search(str(value)) is not None for value in item.values()
                        )
                        else None
                    ),
                )

        for selector_name, field_name in (
            ("depot_arrays", "url"),
            ("storehouse_arrays", "sourceUrl"),
        ):
            for pointer in selectors[selector_name]:
                for index, item in enumerate(_pointer(root, str(pointer))):
                    location = _evidence(self.source, revision, path, f"{pointer}/{index}")
                    raw_target = item.get(field_name) if isinstance(item, Mapping) else None
                    if not isinstance(raw_target, str) or not raw_target.strip():
                        self._add(
                            kind="nested_config",
                            identity=_hidden_identity(repr(raw_target)),
                            public_url=None,
                            evidence=location,
                            failure_reason=FailureReason.SCHEMA_INCOMPATIBLE,
                        )
                        continue
                    from urllib.parse import urljoin

                    self._nested(
                        urljoin(base_url, raw_target),
                        location,
                        depth=depth,
                        queue=queue,
                    )

        for pointer in selectors["live_arrays"]:
            for index, item in enumerate(_pointer(root, str(pointer))):
                location = _evidence(self.source, revision, path, f"{pointer}/{index}")
                raw_target = item.get("url") if isinstance(item, Mapping) else item
                if not isinstance(raw_target, str) or not raw_target.strip():
                    self._add(
                        kind="nested_config",
                        identity=_hidden_identity(repr(raw_target)),
                        public_url=None,
                        evidence=location,
                        failure_reason=FailureReason.SCHEMA_INCOMPATIBLE,
                    )
                    continue
                from urllib.parse import urljoin

                self._add_url_candidate(
                    kind="nested_config",
                    raw_target=urljoin(base_url, raw_target),
                    evidence=location,
                )

    def _process_playlist(
        self,
        body: bytes,
        *,
        parser: str,
        base_url: str,
        path: str,
    ) -> None:
        playlist = (
            parse_m3u(body, base_url=base_url)
            if parser == "m3u"
            else parse_txt_live(body, base_url=base_url)
        )
        revision = self.github.resolved_revision
        for index, entry in enumerate(playlist.entries):
            self._add_url_candidate(
                kind="live_url",
                raw_target=entry.url,
                evidence=_evidence(self.source, revision, path, f"/entries/{index}"),
                header=entry.declared_headers.values if entry.declared_headers else None,
            )

    def run(self) -> CatalogScanResult:
        tree = fetch_github_tree(self.source, self.github, self.client)
        path_patterns = tuple(_glob_regex(str(pattern)) for pattern in self.contract["path_globs"])
        initial_paths = [
            entry.path
            for entry in tree
            if entry.object_type == "blob"
            and any(pattern.fullmatch(entry.path) for pattern in path_patterns)
        ]
        if len(initial_paths) > self.max_files:
            raise _CatalogLimit
        queue: deque[tuple[str, int]] = deque((path, 0) for path in initial_paths)
        visited: set[tuple[str, str, str]] = set()
        while queue:
            path, depth = queue.popleft()
            key = (
                self.github.repository_url,
                self.github.resolved_revision,
                path,
            )
            if key in visited:
                continue
            visited.add(key)
            self.files_scanned += 1
            if self.files_scanned > self.max_files:
                raise _CatalogLimit
            parser = _parser_for_path(path, self.mappings)
            base_url, body = fetch_github_file(
                self.source,
                self.github,
                path,
                self.client,
                max_bytes=10 * 1024 * 1024 if parser in {"m3u", "txt_live"} else 5 * 1024 * 1024,
            )
            if parser in {"tvbox_json", "tvbox_json5"}:
                self._process_json(
                    body,
                    parser=parser,
                    base_url=base_url,
                    path=path,
                    depth=depth,
                    queue=queue,
                )
            else:
                self._process_playlist(
                    body,
                    parser=parser,
                    base_url=base_url,
                    path=path,
                )

        candidates = tuple(
            CatalogCandidate(
                candidate_id=value.candidate_id,
                kind=value.kind,
                normalized_target_hash=value.normalized_target_hash,
                technical_status=value.technical_status,
                rights_status=RightsStatus.UNKNOWN,
                publication_status=PublicationStatus.WITHHELD,
                evidence_locations=tuple(
                    sorted(value.evidence, key=lambda item: item.encode("utf-8"))
                ),
                failure_reason=value.failure_reason,
                secondary_reasons=tuple(
                    sorted(value.secondary_reasons, key=lambda reason: reason.value)
                ),
                public_url=value.public_url,
            )
            for _, value in sorted(self.candidates.items())
        )
        reason = FailureReason.CATALOG_DEPTH_EXCEEDED if self.depth_exceeded else None
        return CatalogScanResult(
            source_id=self.source.id,
            reviewed_revision=self.source.fetch.reviewed_revision or "",
            resolved_revision=self.github.resolved_revision,
            technical_status=(
                TechnicalStatus.PARTIAL if self.depth_exceeded else TechnicalStatus.HEALTHY
            ),
            publication_status=PublicationStatus.WITHHELD,
            inconclusive=False,
            files_scanned=self.files_scanned,
            candidates=candidates,
            failure_reason=reason,
        )


def scan_catalog(
    source: SourceSpec,
    snapshot: UpstreamSnapshot,
    client: Fetcher,
) -> CatalogScanResult:
    """Scan a catalog snapshot without ever returning publishable entities."""

    if source.kind.value != "repository_catalog":
        raise ContractError("scan_catalog requires a repository_catalog source")
    if source.fetch.reviewed_revision is None or snapshot.github is None:
        raise ContractError("catalog source has no GitHub snapshot")
    scanner = _Scanner(source, snapshot, client)
    try:
        return scanner.run()
    except _CatalogLimit:
        candidates = tuple(
            CatalogCandidate(
                candidate_id=value.candidate_id,
                kind=value.kind,
                normalized_target_hash=value.normalized_target_hash,
                technical_status=value.technical_status,
                rights_status=RightsStatus.UNKNOWN,
                publication_status=PublicationStatus.WITHHELD,
                evidence_locations=tuple(sorted(value.evidence)),
                failure_reason=value.failure_reason,
                secondary_reasons=tuple(
                    sorted(value.secondary_reasons, key=lambda item: item.value)
                ),
                public_url=value.public_url,
            )
            for _, value in sorted(scanner.candidates.items())
        )
        return CatalogScanResult(
            source_id=source.id,
            reviewed_revision=source.fetch.reviewed_revision,
            resolved_revision=snapshot.github.resolved_revision,
            technical_status=TechnicalStatus.UNKNOWN,
            publication_status=PublicationStatus.WITHHELD,
            inconclusive=True,
            files_scanned=scanner.files_scanned,
            candidates=candidates,
            failure_reason=FailureReason.CATALOG_LIMIT_EXCEEDED,
        )
    except UpstreamFailure as exc:
        return CatalogScanResult(
            source_id=source.id,
            reviewed_revision=source.fetch.reviewed_revision,
            resolved_revision=snapshot.github.resolved_revision,
            technical_status=TechnicalStatus.DEAD,
            publication_status=PublicationStatus.WITHHELD,
            inconclusive=False,
            files_scanned=scanner.files_scanned,
            candidates=(),
            failure_reason=exc.reason,
            secondary_reasons=exc.secondary_reasons,
        )
