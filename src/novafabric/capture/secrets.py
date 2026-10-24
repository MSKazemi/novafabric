from __future__ import annotations

import codecs
import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from novafabric._capsule_fs import CapsuleEntryError, open_capsule_file, replace_capsule_file
from novafabric.capture._ulid import new_ulid

logger = logging.getLogger(__name__)

PACK_NAME = "gitleaks-core-v0"
# 0.5.0: digest/UUID false-positive guards (ADR-0261).
# 0.6.0: real key formats matched in full -- OpenAI project/service/admin keys
#        (`sk-proj-` etc., previously not matched at all), Anthropic keys longer
#        than 87 chars (previously masked with the tail left in clear), Langfuse
#        secret keys (`sk-lf-`; only the public `pk-lf-` matched before).
# 0.7.0: AWS and GitHub credentials, which ADR-0009 names (gitleaks) but the pack
#        never carried -- `aws-access-key-id` (AKIA/ASIA/ABIA/ACCA/A3T*),
#        `aws-secret-access-key` (only when anchored by its key name; the value
#        alone is an unprefixed 40-char run), `github-token` (ghp_/gho_/ghu_/
#        ghs_/ghr_) and `github-fine-grained-pat` (github_pat_).
PACK_VERSION = "0.7.0"
#                        0.4.0: + novafabric-webhook-secret (nvwh_, ADR-0205)

# 18 key patterns — ordered from most to least specific to avoid false positives.
# A rule may set ``secret_group``: only that capture group is the secret -- it is
# what ``match_hash`` hashes and what is replaced; the rest of the match is the
# context that anchors it and stays in the capsule. Absent means the whole match.
_RULES: list[dict[str, Any]] = [
    # ADR-0193: our own credential format (`nvfk_<key_id>_<secret>`) — detect a
    # leaked NovaFabric API key in a capsule before anyone else does.
    {"id": "novafabric-api-key", "severity": "critical",
     "pattern": re.compile(r"nvfk_[A-Za-z0-9\-_]{8}_[A-Za-z0-9\-_]{30,60}")},
    # ADR-0205: webhook signing secret (`nvwh_<hook_id>_<secret>`) — same
    # posture as nvfk_ for our second first-party credential format.
    {"id": "novafabric-webhook-secret", "severity": "critical",
     "pattern": re.compile(r"nvwh_[A-Za-z0-9\-_]{8}_[A-Za-z0-9\-_]{30,60}")},
    # No upper length bound on key bodies: a bound stops the match mid-key and
    # leaves the remainder in clear next to the [REDACTED] marker (pack 0.6.0).
    {"id": "anthropic-api-key", "severity": "critical",
     "pattern": re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")},
    {"id": "openai-api-key", "severity": "critical",
     "pattern": re.compile(
         r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9\-_]{20,}"
         r"|sk-(?!ant-)[A-Za-z0-9]{20,}"
     )},
    {"id": "huggingface-token", "severity": "high",
     "pattern": re.compile(r"hf_[A-Za-z0-9]{34,50}")},
    {"id": "replicate-api-key", "severity": "high",
     "pattern": re.compile(r"r8_[A-Za-z0-9]{37}")},
    {"id": "langfuse-key", "severity": "medium",
     "pattern": re.compile(r"[ps]k-lf-[A-Za-z0-9\-]{30,50}")},
    {"id": "langsmith-key", "severity": "medium",
     "pattern": re.compile(r"ls__[A-Za-z0-9]{40,60}")},
    {"id": "weaviate-api-key", "severity": "medium",
     "pattern": re.compile(r"wcs_[A-Za-z0-9]{30,50}")},
    {"id": "qdrant-api-key", "severity": "medium",
     "pattern": re.compile(r"qdrant_[A-Za-z0-9]{30,50}")},
    # Pack 0.7.0 (gitleaks `aws-access-token`): an access key id identifies the
    # account and is half of the credential pair.
    {"id": "aws-access-key-id", "severity": "high",
     "pattern": re.compile(
         r"(?<![A-Za-z0-9])(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}(?![A-Za-z0-9])"
     )},
    # The secret access key has no prefix: alone it is any 40-char base64 run, so a
    # bare-value rule would redact digests and ids (the ADR-0261 failure class). It
    # is matched only when its key name anchors it -- env/ini/YAML/JSON (including
    # JSON-escaped quotes inside a JSONL string), boto kwargs, STS `SecretAccessKey`
    # output and `--aws-secret-access-key <value>`. Only the value is redacted.
    {"id": "aws-secret-access-key", "severity": "critical",
     "secret_group": 1,
     "pattern": re.compile(
         r"(?i:(?:aws[_-]?)?secret[_-]?access[_-]?key|aws[_-]?secret[_-]?key)"
         r"(?:[\s\\\"']*[:=]+[\s\\\"']*|\s+)"
         r"([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
     )},
    # Pack 0.7.0 (gitleaks `github-pat`, `github-oauth`, `github-app-token`,
    # `github-refresh-token`, `github-fine-grained-pat`). No upper length bound,
    # for the reason given above the Anthropic rule.
    {"id": "github-token", "severity": "critical",
     "pattern": re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}")},
    {"id": "github-fine-grained-pat", "severity": "critical",
     "pattern": re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{22,}")},
    # False-positive guard (pack 0.5.0, ADR-0261): a bare 40-character token that
    # is entirely lowercase hex is a SHA-1 -- a git commit id, a blob digest --
    # not a Cohere key. `git rev-parse HEAD` is an ordinary coding-agent tool
    # call, so without this guard every commit id in a capsule is destroyed.
    # A real Cohere key draws 40 characters from a 62-character alphabet; the
    # probability that one is all lowercase hex is (16/62)^40 ~ 1e-24, so the
    # recall cost is not measurable. Same reasoning as ADR-0125 below.
    {"id": "cohere-api-key", "severity": "high",
     "pattern": re.compile(
         r"(?<![A-Za-z0-9])(?![0-9a-f]{40}(?![A-Za-z0-9]))[A-Za-z0-9]{40}(?![A-Za-z0-9])"
     )},
    # False-positive guard (pack 0.2.1, ADR-0125): a bare 64-hex string that is
    # a capsule content address — prefixed "sha256:" (Artifact/MediaPart
    # content_hash) or "outputs/" (content-addressed blob ref) — is NOT a
    # Together API key. Real keys never carry those prefixes.
    {"id": "together-api-key", "severity": "high",
     "pattern": re.compile(
         r"(?<![0-9a-f])(?<!sha256:)(?<!outputs/)[0-9a-f]{64}(?![0-9a-f])"
     )},
    # False-positive guard (pack 0.5.0, ADR-0261): a bare 32-character token that
    # is entirely lowercase hex is an MD5 digest, not a Mistral key. Recall cost
    # is (16/62)^32 ~ 4e-19.
    {"id": "mistral-api-key", "severity": "high",
     "pattern": re.compile(
         r"(?<![A-Za-z0-9])(?![0-9a-f]{32}(?![A-Za-z0-9]))[A-Za-z0-9]{32}(?![A-Za-z0-9])"
     )},
    # ADR-0261. This rule previously matched a bare UUID. A Pinecone legacy key
    # IS a UUID and a NovaFabric run id IS a UUID: they are structurally
    # identical, so no pattern can separate them, and the old rule therefore
    # redacted every run, capsule and trace identifier it saw. For an evidence
    # system whose capsules are addressed by those identifiers that is the more
    # damaging error, so the rule now matches only Pinecone's prefixed formats
    # -- `pckey_` (current) and `pcsk_` (legacy) -- which are unambiguous.
    # Residual risk, stated rather than hidden: a pre-prefix bare-UUID Pinecone
    # key is NOT detected by this rule. Configure a custom rule if you still
    # issue them.
    {"id": "pinecone-api-key", "severity": "medium",
     "pattern": re.compile(r"pc(?:key|sk)_[A-Za-z0-9\-_]{16,120}")},
]

_PACK_RULES_HASH = "sha256:" + hashlib.sha256(
    "|".join(str(r["id"]) for r in _RULES).encode()
).hexdigest()

_SCAN_TARGETS = [
    ("model-calls.jsonl", "model-call-messages"),
    ("tool-calls.jsonl", "tool-call-arguments"),
    ("trace.jsonl", "trace"),
    ("capsule.yaml", "capsule-yaml"),
    # ADR-0209 D5.1: every extended event stream the `novafabric.capture.record`
    # façade (or a default-path wiring) can populate with free text is scanned
    # at finalize like everything else — plus network_events / human_approvals,
    # which carry URLs and rationale text and were equally uncovered before.
    # Absent streams cost one stat() each; clean capsules are unchanged.
    ("file_events.jsonl", "file-events"),
    ("network_events.jsonl", "network-events"),
    ("human_approvals.jsonl", "human-approvals"),
    ("state_transitions.jsonl", "state-transitions"),
    ("memory_operations.jsonl", "memory-operations"),
    ("guardrail_events.jsonl", "guardrail-events"),
    ("evaluator_events.jsonl", "evaluator-events"),
    ("reranker_events.jsonl", "reranker-events"),
    ("vector_retrievals.jsonl", "vector-retrievals"),
]

# Public alias: the structured streams. The ADR-0135 masking pipeline walks these
# AND the artifact targets below -- the same set the built-in scanner walks (see
# ``iter_artifact_targets``) -- and the content index bounds its corpus by it.
SCAN_TARGETS: list[tuple[str, str]] = _SCAN_TARGETS

# ADR-0009 "Scanning targets": every byte written to the capsule is scanned, not
# only the structured streams above. These are the remaining files the ADR names.
# Kept separate from SCAN_TARGETS because that list is the content-index corpus
# bound (ADR-0204), which must not grow to raw stdout/stderr by accident.
ARTIFACT_SCAN_TARGETS: list[tuple[str, str]] = [
    ("env.lock", "env-lock"),
    ("assets.jsonl", "assets"),
    ("lineage.jsonl", "lineage"),
]
# Directories walked recursively (symlinks are never followed).
ARTIFACT_SCAN_DIRS: list[tuple[str, str]] = [
    ("inputs", "input-artifact"),
    ("outputs", "output-artifact"),
]
# Bounded work: an artifact larger than this is not read into memory. It is
# recorded in the proof as skipped (with the reason), never silently passed.
MAX_ARTIFACT_SCAN_BYTES = 64 * 1024 * 1024

# ADR-0009: binaries are scanned via string extraction (ASCII runs >= 8 chars).
_BINARY_STRING_RE = re.compile(rb"[\x20-\x7e]{8,}")
_EMPTY_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()
# A binary with a match is DROPPED, so only rules with a distinctive key prefix may
# decide it. These three match any 40/32-char alphanumeric or 64-hex run -- e.g. a
# PDF's uppercase-hex /ID -- and would delete ordinary binaries (media blobs that
# model-calls.jsonl references by hash). They still apply to text artifacts, where
# a false positive only masks a string.
_BINARY_EXCLUDED_RULES = frozenset({"cohere-api-key", "together-api-key", "mistral-api-key"})
# The rules allowed to DROP a file: on binary content, and on a file *name*. A path
# like `outputs/media/<64-hex>` must never be dropped by the bare-hex Together rule.
_DROP_RULES: list[dict[str, Any]] = [
    r for r in _RULES if r["id"] not in _BINARY_EXCLUDED_RULES
]

# Residual pass (ADR-0009 "every byte written to the capsule"): a file in the
# finished capsule that none of the lists above names is still rescanned, and is
# recorded under this kind. `capsule.yaml` is checked separately (it carries the
# digest map, so it is written last); `.seal/` does not exist yet; the proof is
# the output of the pass, not an input to it.
OTHER_FILE_KIND = "capsule-file"
_RESIDUAL_EXCLUDED: frozenset[str] = frozenset({"capsule.yaml", "redaction-proof.json"})
_RESIDUAL_EXCLUDED_DIRS: frozenset[str] = frozenset({".seal"})


class ResidualSecretError(Exception):
    """A supported secret pattern is still present in a finished capsule file.

    Raised by :meth:`SecretScannerV0.assert_manifest_clean`, the last gate before the
    manifest is written and sealed. Carries the file and the rule ids that fired,
    never the matched value -- this message is destined for a log.
    """

    def __init__(self, ref: str, rule_ids: list[str]) -> None:
        self.ref = ref
        self.rule_ids = sorted(set(rule_ids))
        super().__init__(
            f"{ref}: residual secret match after redaction (rules: {', '.join(self.rule_ids)})"
        )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _count_by_severity(findings: list[dict[str, Any]]) -> dict[str, int]:
    by_severity: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        sev = str(f["severity"])
        if sev in by_severity:
            by_severity[sev] += 1
    return by_severity


def _fold_target(
    targets: list[dict[str, Any]],
    by_ref: dict[str, dict[str, Any]],
    target: dict[str, Any],
    new_findings: int,
) -> bool:
    """Fold a rescan ``target`` into ``targets`` in place.

    A target already recorded keeps its ``hash_before_redaction`` (the original
    bytes -- ADR-0009 semantics); its ``hash_after_redaction`` moves to the bytes
    as rescanned and ``findings_count`` grows by ``new_findings``. An unrecorded
    one is appended. Returns True when a recorded target's bytes had changed
    since its scan with nothing found now -- a clean rewrite, e.g. by a masker.
    """
    ref = str(target["ref"])
    prior = by_ref.get(ref)
    if prior is None:
        targets.append(target)
        by_ref[ref] = target
        return False
    reconciled = (
        new_findings == 0
        and prior["hash_after_redaction"] != target["hash_after_redaction"]
    )
    if new_findings:
        prior["findings_count"] = int(prior["findings_count"]) + new_findings
        prior["binary"] = bool(prior["binary"]) or bool(target["binary"])
    prior["hash_after_redaction"] = target["hash_after_redaction"]
    return reconciled


#: Proof fields that name a capsule path. They are judged by the anchored rules
#: only, as file names are (``_name_findings``): a generic rule would mask a
#: legitimate hex/alnum file name here while the same name stays a key of
#: ``evidence_digests``, and the proof would stop naming the file it scanned.
_PATH_FIELDS: frozenset[str] = frozenset({"ref", "target_ref"})


def _redact_strings(value: Any, *, anchored_only: bool = False) -> tuple[Any, int]:
    """Mask every rule match in every string (keys included) of a JSON-like value.

    Returns ``(new_value, number_of_strings_changed)``; the input is not mutated.
    """
    if isinstance(value, str):
        new = value
        for rule in _DROP_RULES if anchored_only else _RULES:
            new = _sub_rule(rule, new, "mask")
        return new, int(new != value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        changed = 0
        for k, v in value.items():
            new_k, ck = _redact_strings(k)
            new_v, cv = _redact_strings(
                v, anchored_only=anchored_only or k in _PATH_FIELDS
            )
            out[new_k] = new_v
            changed += ck + cv
        return out, changed
    if isinstance(value, list):
        items: list[Any] = []
        changed = 0
        for v in value:
            new_v, cv = _redact_strings(v, anchored_only=anchored_only)
            items.append(new_v)
            changed += cv
        return items, changed
    return value, 0


def iter_artifact_targets(capsule_dir: Path) -> Iterator[tuple[Path, str, str]]:
    """``(path, ref, kind)`` for every ADR-0009 artifact target present in a capsule.

    The fixed files of :data:`ARTIFACT_SCAN_TARGETS`, then every regular file under
    :data:`ARTIFACT_SCAN_DIRS`, sorted. Symlinks are never followed or yielded. One
    enumeration shared by the built-in scanner and the ADR-0135 masking pipeline, so
    the two cannot walk different sets.
    """
    for filename, kind in ARTIFACT_SCAN_TARGETS:
        path = capsule_dir / filename
        if path.is_file() and not path.is_symlink():
            yield path, filename, kind
    for dirname, kind in ARTIFACT_SCAN_DIRS:
        root = capsule_dir / dirname
        if not root.is_dir() or root.is_symlink():
            continue
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink():
                    found.append(path)
        for path in sorted(found):
            yield path, path.relative_to(capsule_dir).as_posix(), kind


_VALID_STRATEGIES = {"mask", "hash", "drop"}


def _replacement(rule_id: str, strategy: str, matched: str) -> str:
    if strategy == "mask":
        return f"[REDACTED:{rule_id}]"
    if strategy == "hash":
        first8 = hashlib.sha256(matched.encode()).hexdigest()[:8]
        return f"[REDACTED:{rule_id}:sha256:{first8}]"
    if strategy == "drop":
        return ""
    raise ValueError(f"unknown strategy: {strategy!r}")


def _secret_span(rule: dict[str, Any], m: re.Match[str]) -> tuple[int, str]:
    """``(offset, secret)`` of a match: the rule's ``secret_group``, else the whole match."""
    group = int(rule.get("secret_group", 0))
    return m.start(group), m.group(group)


def _sub_rule(rule: dict[str, Any], text: str, strategy: str) -> str:
    """Replace every match of one rule in ``text``; context outside ``secret_group`` stays."""
    rule_id = str(rule["id"])
    group = int(rule.get("secret_group", 0))

    def _repl(m: re.Match[str]) -> str:
        whole, base = m.group(), m.start()
        start, end = m.span(group)
        return (
            whole[: start - base]
            + _replacement(rule_id, strategy, m.group(group))
            + whole[end - base :]
        )

    return str(rule["pattern"].sub(_repl, text))


def redact_secrets_in_text(text: str) -> str:
    """Mask every rule match in ``text`` — same pack the capsule scanner uses.

    Reused by the lifecycle-event emitter (ADR-0137 D5) as the
    scan-before-emit payload-hygiene pass. Always applies the ``mask``
    strategy; returns the redacted text.
    """
    for rule in _RULES:
        text = _sub_rule(rule, text, "mask")
    return text


def redact_json_strings(value: Any) -> Any:
    """A copy of a JSON-like value with every rule match masked in every string.

    Same rule pack and ``mask`` placeholder (``[REDACTED:<rule>]``) as the capsule
    scanner. ADR-0306 digests ``record.tool`` arguments and results over this
    redacted form, so a detected secret contributes only its placeholder and a
    stored digest is never an offline guessing oracle for it (the NF-166 lesson).
    """
    return _redact_strings(value)[0]


def scan_text_rule_ids(text: str) -> list[str]:
    """Rule ids from the ADR-0009 pack that match *text*, in pack order.

    The text-level counterpart of :func:`redact_secrets_in_text`, which transforms but does
    not report. :class:`SecretScannerV0` is file-oriented — it walks ``_SCAN_TARGETS`` inside
    a capsule directory — so a caller holding a bare string had no way to ask *which* rule
    fired. NF-166 needs exactly that: on a match it suppresses the typed-text digest
    entirely, and the reason has to name the rule.

    One rule set, one reader: this iterates the same ``_RULES`` the scanner and the redactor
    use, so a new rule reaches all three at once.
    """
    return [str(rule["id"]) for rule in _RULES if rule["pattern"].search(text)]


def recompute_chain_hash(proof: dict[str, Any]) -> dict[str, Any]:
    """Recompute chain_hash over a proof's canonical JSON. Returns the same dict."""
    proof = dict(proof)
    proof.pop("chain_hash", None)
    canonical = json.dumps(proof, sort_keys=True, separators=(",", ":"))
    proof["chain_hash"] = _sha256(canonical.encode())
    return proof


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


#: Window and overlap for streaming an artifact over MAX_ARTIFACT_SCAN_BYTES.
#: The overlap bounds the longest match that is always seen whole.
_STREAM_WINDOW_BYTES = 8 * 1024 * 1024
_STREAM_OVERLAP_BYTES = 64 * 1024
_WHITESPACE_RE = re.compile(r"\s")
#: How many windows of whitespace-free text are carried before cutting inside it.
_STREAM_MAX_CARRY_WINDOWS = 4


def _looks_binary(head: bytes) -> bool:
    """:func:`_is_binary` for the first window of a stream (a cut UTF-8 sequence is fine)."""
    if b"\x00" in head:
        return True
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return True
    return False


def _byte_windows(fh: BinaryIO, digest: Any) -> Iterator[tuple[int, bytes, bool]]:
    """``(offset, window, final)`` over *fh*: windows overlapping by the stream overlap."""
    carry = b""
    base = 0
    step = max(1, _STREAM_WINDOW_BYTES - _STREAM_OVERLAP_BYTES)
    while True:
        raw = fh.read(step if carry else _STREAM_WINDOW_BYTES)
        if digest is not None:
            digest.update(raw)
        window = carry + raw
        final = not raw or len(raw) < (step if carry else _STREAM_WINDOW_BYTES)
        if window:
            yield base, window, final
        if final:
            return
        keep = min(len(window), _STREAM_OVERLAP_BYTES)
        base += len(window) - keep
        carry = window[len(window) - keep:]


def _text_segments(fh: BinaryIO, digest: Any) -> Iterator[str]:
    """Decoded text of *fh* as consecutive segments that tile it exactly.

    Each segment ends at least :data:`_STREAM_OVERLAP_BYTES` before the decoded
    window's end, extended to the end of any match that starts before that point,
    so a match up to the overlap's length is never split. *digest*, if given, is
    updated with the raw bytes.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    carry = ""
    while True:
        raw = fh.read(_STREAM_WINDOW_BYTES)
        if digest is not None:
            digest.update(raw)
        final = not raw
        text = carry + decoder.decode(raw, final=final)
        if final:
            if text:
                yield text
            return
        safe = max(0, len(text) - _STREAM_OVERLAP_BYTES)
        cut = safe
        # Only a match starting within one overlap of *safe* can cross it (the
        # overlap bounds the match length); a compiled pattern given *pos* still
        # sees the text before it for look-behinds and word boundaries.
        lookback = max(0, safe - _STREAM_OVERLAP_BYTES)
        for rule in _RULES:
            for m in rule["pattern"].finditer(text, lookback):
                if m.start() >= safe:
                    break
                if m.end() > cut:
                    cut = m.end()
        # Cut just after whitespace: rules anchor on word boundaries and
        # look-behinds, so a cut inside a run of word characters would give the
        # next segment a different context than the whole file has.
        space = _WHITESPACE_RE.search(text, cut)
        if space is not None:
            cut = space.end()
        elif len(text) < _STREAM_MAX_CARRY_WINDOWS * _STREAM_WINDOW_BYTES:
            cut = 0  # no clean cut yet: carry the whole window into the next read
        # else: a whitespace-free run longer than the carry bound is cut where
        # it is (documented residual: matches at that one point may differ).
        if cut:
            yield text[:cut]
        carry = text[cut:]


def _stream_hash(fh: BinaryIO) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
    return "sha256:" + h.hexdigest()


#: The scanner's ``operation`` name in a ``_capsule_fs`` refusal.
_SCAN_OPERATION = "the secret scanner"


def merge_scan_results(
    proof: dict[str, Any],
    targets: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    bytes_redacted: int = 0,
) -> dict[str, Any]:
    """Fold later scan results into an existing proof and re-chain it.

    A target whose ``ref`` is already in the proof is replaced (with its
    findings), so a file scanned twice is reported once, as finally written.
    """
    proof = dict(proof)
    new_refs = {t["ref"] for t in targets}
    kept_targets = [t for t in proof.get("targets", []) if t["ref"] not in new_refs]
    kept_findings = [f for f in proof.get("findings", []) if f["target_ref"] not in new_refs]
    all_findings = kept_findings + list(findings)
    proof["targets"] = kept_targets + list(targets)
    proof["findings"] = all_findings
    proof["findings_count"] = {
        "total": len(all_findings), "by_severity": _count_by_severity(all_findings)
    }
    proof["bytes_scanned"] = sum(int(t["bytes_scanned"]) for t in proof["targets"])
    proof["bytes_redacted"] = int(proof.get("bytes_redacted", 0)) + max(0, bytes_redacted)
    return recompute_chain_hash(proof)


class SecretScannerV0:
    def __init__(
        self,
        capsule_dir: Path,
        run_id: str,
        strategy_overrides: dict[str, str] | None = None,
    ) -> None:
        self._dir = capsule_dir
        self._run_id = run_id
        self._overrides: dict[str, str] = {}
        for rule_id, strategy in (strategy_overrides or {}).items():
            if strategy not in _VALID_STRATEGIES:
                raise ValueError(
                    f"invalid strategy {strategy!r} for rule {rule_id!r}; "
                    f"expected one of {sorted(_VALID_STRATEGIES)}"
                )
            self._overrides[rule_id] = strategy

    def _strategy_for(self, rule_id: str) -> str:
        return self._overrides.get(rule_id, "mask")

    # -- finding construction -------------------------------------------------

    def _finding(
        self,
        rule: dict[str, Any],
        kind: str,
        ref: str,
        offset: int,
        matched: str,
        strategy: str,
    ) -> dict[str, Any]:
        rule_id = str(rule["id"])
        return {
            "finding_id": new_ulid(),
            "rule_id": rule_id,
            "rule_version": "0.1.0",
            "pack": PACK_NAME,
            "severity": rule["severity"],
            "target_kind": kind,
            "target_ref": ref,
            "byte_offset": offset,
            "byte_length": len(matched.encode()),
            "match_hash": _sha256(matched.encode()),
            "redaction_strategy": strategy,
            "replacement": _replacement(rule_id, strategy, matched),
        }

    def _text_findings(self, content: str, kind: str, ref: str) -> list[dict[str, Any]]:
        # Offsets are on the original content, before any substitution.
        out: list[dict[str, Any]] = []
        for rule in _RULES:
            strategy = self._strategy_for(str(rule["id"]))
            for m in rule["pattern"].finditer(content):
                offset, secret = _secret_span(rule, m)
                out.append(self._finding(rule, kind, ref, offset, secret, strategy))
        return out

    def _redact_text(self, content: str) -> str:
        for rule in _RULES:
            content = _sub_rule(rule, content, self._strategy_for(str(rule["id"])))
        return content

    @staticmethod
    def _target(
        kind: str,
        ref: str,
        *,
        bytes_scanned: int,
        findings_count: int,
        hash_before: str,
        hash_after: str,
        binary: bool = False,
        skip_reason: str | None = None,
    ) -> dict[str, Any]:
        target: dict[str, Any] = {
            "kind": kind,
            "ref": ref,
            "bytes_scanned": bytes_scanned,
            "findings_count": findings_count,
            "skipped": skip_reason is not None,
            "binary": binary,
            "hash_before_redaction": hash_before,
            "hash_after_redaction": hash_after,
        }
        if skip_reason is not None:
            target["skip_reason"] = skip_reason
        return target

    # -- target enumeration ---------------------------------------------------

    def _artifact_targets(self) -> Iterator[tuple[Path, str, str]]:
        return iter_artifact_targets(self._dir)

    def _all_capsule_files(self) -> Iterator[tuple[Path, str, str, bool]]:
        """``(path, ref, kind, structured)`` for every file the residual pass rescans.

        Every regular file in the capsule except :data:`_RESIDUAL_EXCLUDED` (top
        level) and anything under ``.seal/``; symlinks are neither followed nor
        yielded. ``structured`` marks the streams the main pass scans as text
        (``_scan_structured``) rather than as artifacts that may be binary.
        """
        structured = dict(_SCAN_TARGETS)
        fixed = dict(ARTIFACT_SCAN_TARGETS)
        dirs = dict(ARTIFACT_SCAN_DIRS)
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self._dir, followlinks=False):
            if Path(dirpath) == self._dir:
                dirnames[:] = [d for d in dirnames if d not in _RESIDUAL_EXCLUDED_DIRS]
            dirnames.sort()
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink():
                    found.append(path)
        for path in sorted(found):
            ref = path.relative_to(self._dir).as_posix()
            if ref in _RESIDUAL_EXCLUDED:
                continue
            if ref in structured:
                yield path, ref, structured[ref], True
            elif ref in fixed:
                yield path, ref, fixed[ref], False
            else:
                top = ref.split("/", 1)[0]
                kind = dirs.get(top, OTHER_FILE_KIND) if "/" in ref else OTHER_FILE_KIND
                yield path, ref, kind, False

    # -- file names -----------------------------------------------------------

    def _name_findings(self, ref: str, kind: str) -> tuple[str, list[dict[str, Any]]]:
        """Findings for a key-shaped secret in a file's *path*, and the redacted ref.

        A path is not file content, but it is written into the capsule twice: as a
        ``ref`` in this proof and as a key of the manifest's ``evidence_digests``.
        Only :data:`_DROP_RULES` apply -- a content-addressed name such as
        ``outputs/media/<64-hex>`` must never trip the bare-hex rule. Offsets are
        into the path string.
        """
        findings: list[dict[str, Any]] = []
        safe_ref = ref
        for rule in _DROP_RULES:
            for m in rule["pattern"].finditer(ref):
                offset, secret = _secret_span(rule, m)
                findings.append(self._finding(rule, kind, ref, offset, secret, "drop"))
            safe_ref = _sub_rule(rule, safe_ref, "mask")
        for f in findings:
            f["target_ref"] = safe_ref
        return safe_ref, findings

    def _drop_named(
        self,
        path: Path,
        fh: BinaryIO,
        safe_ref: str,
        kind: str,
        findings: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        size = os.fstat(fh.fileno()).st_size
        digest = _stream_hash(fh)
        path.unlink()
        logger.warning(
            "novafabric.secrets: artifact %s has a secret in its file name and was "
            "dropped from the capsule",
            safe_ref,
        )
        return (
            self._target(
                kind, safe_ref,
                bytes_scanned=0,
                findings_count=len(findings),
                hash_before=digest,
                hash_after=_EMPTY_HASH,
            ),
            findings,
            size,
        )

    # -- per-file scanning ----------------------------------------------------

    def _open(self, ref: str) -> BinaryIO:
        """Open capsule file *ref* for reading: no link followed, no FIFO opened.

        The capsule is untrusted input once the workload has run (it may be
        bind-mounted into a container), so a symlink planted at a stream is
        refused here rather than read -- and therefore never rewritten through.
        """
        return open_capsule_file(self._dir, ref, operation=_SCAN_OPERATION)

    def _refused(
        self, ref: str, kind: str, exc: BaseException
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        """A skipped target for a file that is not a regular file: never read or rewritten."""
        reason = f"stream_tampered: {ref} is not a regular file and was not read ({exc})"
        logger.warning("novafabric.secrets: %s", reason)
        return (
            self._target(
                kind, ref,
                bytes_scanned=0,
                findings_count=0,
                hash_before=_EMPTY_HASH,
                hash_after=_EMPTY_HASH,
                skip_reason=reason[:500],
            ),
            [],
            0,
        )

    def _scan_structured(
        self, path: Path, ref: str, kind: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        try:
            with self._open(ref) as fh:
                file_mode = stat.S_IMODE(os.fstat(fh.fileno()).st_mode)
                original = fh.read()
        except (CapsuleEntryError, IsADirectoryError, NotADirectoryError) as exc:
            return self._refused(ref, kind, exc)
        return self._scan_text_bytes(ref, kind, original, file_mode)

    def _scan_text_bytes(
        self, ref: str, kind: str, original: bytes, file_mode: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        content = original.decode("utf-8", errors="replace")
        findings = self._text_findings(content, kind, ref)
        redacted = self._redact_text(content).encode("utf-8")
        removed = 0
        if redacted != original:
            # An atomic rename: whatever is at *ref* now (a link swapped in
            # after the read included) is replaced, never written through.
            replace_capsule_file(
                self._dir, ref, redacted, operation=_SCAN_OPERATION, file_mode=file_mode
            )
            removed = max(0, len(original) - len(redacted))
        return (
            self._target(
                kind, ref,
                bytes_scanned=len(original),
                findings_count=len(findings),
                hash_before=_sha256(original),
                hash_after=_sha256(redacted),
            ),
            findings,
            removed,
        )

    def _scan_artifact(
        self, path: Path, ref: str, kind: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        try:
            fh = self._open(ref)
        except (CapsuleEntryError, IsADirectoryError, NotADirectoryError) as exc:
            return self._refused(ref, kind, exc)
        with fh:
            return self._scan_open_artifact(path, fh, ref, kind)

    def _scan_open_artifact(
        self, path: Path, fh: BinaryIO, ref: str, kind: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        safe_ref, name_findings = self._name_findings(ref, kind)
        if name_findings:
            return self._drop_named(path, fh, safe_ref, kind, name_findings)
        st = os.fstat(fh.fileno())
        size = st.st_size
        if size > MAX_ARTIFACT_SCAN_BYTES:
            # Review capture-runners-02: this file was skipped — sealed unscanned,
            # so a key anywhere in a long agent's stdout stayed in the clear. It
            # is too big to hold in memory, so it is scanned in a stream.
            return self._scan_large_artifact(path, fh, ref, kind, size, stat.S_IMODE(st.st_mode))
        original = fh.read()
        if not _is_binary(original):
            return self._scan_text_bytes(ref, kind, original, stat.S_IMODE(st.st_mode))

        # Binary: scan extracted strings; a binary cannot be redacted in place,
        # so one that carries a secret is dropped from the capsule (ADR-0009).
        findings: list[dict[str, Any]] = []
        for run in _BINARY_STRING_RE.finditer(original):
            text = run.group().decode("ascii")
            for rule in _RULES:
                if rule["id"] in _BINARY_EXCLUDED_RULES:
                    continue
                for m in rule["pattern"].finditer(text):
                    offset, secret = _secret_span(rule, m)
                    findings.append(
                        self._finding(rule, kind, ref, run.start() + offset, secret, "drop")
                    )
        hash_before = _sha256(original)
        if not findings:
            return (
                self._target(
                    kind, ref,
                    bytes_scanned=len(original),
                    findings_count=0,
                    hash_before=hash_before,
                    hash_after=hash_before,
                    binary=True,
                ),
                [],
                0,
            )
        path.unlink()
        logger.warning(
            "novafabric.secrets: binary artifact %s contains %d secret match(es) "
            "and was dropped from the capsule",
            ref, len(findings),
        )
        return (
            self._target(
                kind, ref,
                bytes_scanned=len(original),
                findings_count=len(findings),
                hash_before=hash_before,
                hash_after=_EMPTY_HASH,
                binary=True,
            ),
            findings,
            len(original),
        )

    def _scan_large_artifact(
        self, path: Path, fh: BinaryIO, ref: str, kind: str, size: int, file_mode: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        """Scan an artifact over :data:`MAX_ARTIFACT_SCAN_BYTES` without loading it.

        Text is read in :data:`_STREAM_WINDOW_BYTES` windows that overlap by
        :data:`_STREAM_OVERLAP_BYTES` (:func:`_text_segments`), so every match up to
        the overlap's length lies whole in one segment. A first pass collects the
        findings; only if there are any is the file rewritten, by a second pass that
        streams the redacted segments into an atomic replace. A binary is
        string-scanned the same way and, as below the limit, dropped if it carries a
        secret. Residual: a single match longer than the overlap (64 KiB) can be
        split across two segments.
        """
        first = fh.read(_STREAM_WINDOW_BYTES)
        fh.seek(0)
        if _looks_binary(first):
            return self._scan_large_binary(path, fh, ref, kind, size)
        before = hashlib.sha256()
        findings: list[dict[str, Any]] = []
        offset = 0
        for segment in _text_segments(fh, before):
            for rule in _RULES:
                strategy = self._strategy_for(str(rule["id"]))
                for m in rule["pattern"].finditer(segment):
                    at, secret = _secret_span(rule, m)
                    findings.append(self._finding(rule, kind, ref, offset + at, secret, strategy))
            offset += len(segment)
        hash_before = "sha256:" + before.hexdigest()
        if not findings:
            return (
                self._target(
                    kind, ref, bytes_scanned=size, findings_count=0,
                    hash_before=hash_before, hash_after=hash_before,
                ),
                [],
                0,
            )
        fh.seek(0)
        after = hashlib.sha256()
        written = 0

        def redacted() -> Iterator[bytes]:
            nonlocal written
            for segment in _text_segments(fh, None):
                chunk = self._redact_text(segment).encode("utf-8")
                after.update(chunk)
                written += len(chunk)
                yield chunk

        replace_capsule_file(
            self._dir, ref, redacted(), operation=_SCAN_OPERATION, file_mode=file_mode
        )
        return (
            self._target(
                kind, ref, bytes_scanned=size, findings_count=len(findings),
                hash_before=hash_before, hash_after="sha256:" + after.hexdigest(),
            ),
            findings,
            max(0, size - written),
        )

    def _scan_large_binary(
        self, path: Path, fh: BinaryIO, ref: str, kind: str, size: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        """String-scan a large binary in overlapping windows; drop it if it has a secret."""
        before = hashlib.sha256()
        findings: list[dict[str, Any]] = []
        for base, window, final in _byte_windows(fh, before):
            safe = len(window) if final else max(0, len(window) - _STREAM_OVERLAP_BYTES)
            for run in _BINARY_STRING_RE.finditer(window):
                if run.start() >= safe:
                    continue  # seen whole in the next window
                text = run.group().decode("ascii")
                for rule in _RULES:
                    if rule["id"] in _BINARY_EXCLUDED_RULES:
                        continue
                    for m in rule["pattern"].finditer(text):
                        at, secret = _secret_span(rule, m)
                        findings.append(self._finding(
                            rule, kind, ref, base + run.start() + at, secret, "drop"
                        ))
        hash_before = "sha256:" + before.hexdigest()
        if not findings:
            return (
                self._target(
                    kind, ref, bytes_scanned=size, findings_count=0,
                    hash_before=hash_before, hash_after=hash_before, binary=True,
                ),
                [],
                0,
            )
        path.unlink()
        logger.warning(
            "novafabric.secrets: binary artifact %s contains %d secret match(es) "
            "and was dropped from the capsule",
            ref, len(findings),
        )
        return (
            self._target(
                kind, ref, bytes_scanned=size, findings_count=len(findings),
                hash_before=hash_before, hash_after=_EMPTY_HASH, binary=True,
            ),
            findings,
            size,
        )

    def scan_and_redact_refs(
        self, refs: list[tuple[str, str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """Scan specific capsule-relative files (e.g. ones written after the main pass)."""
        targets: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        removed = 0
        for ref, kind in refs:
            path = self._dir / ref
            if not path.is_file() or path.is_symlink():
                continue
            target, file_findings, file_removed = self._scan_artifact(path, ref, kind)
            targets.append(target)
            findings.extend(file_findings)
            removed += file_removed
        return targets, findings, removed

    def redact_manifest(
        self, manifest: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Redact every string value of a manifest before it is written as capsule.yaml.

        Text-level redaction of serialized YAML is unsafe: a secret that is a whole
        scalar would become ``[REDACTED:...]``, which YAML parses as a list. So the
        values are redacted in the data structure, and the findings are reported
        against the YAML the manifest would otherwise have serialized to. String
        mapping *keys* are redacted too: the findings already count a key-shaped
        secret in a key, so leaving it in place would report a redaction that
        never happened.
        Returns ``(redacted_manifest, target, findings)``; the input is not mutated.
        """
        import yaml

        before = yaml.dump(manifest, allow_unicode=True)
        findings = self._text_findings(before, "capsule-yaml", "capsule.yaml")

        def _walk(value: Any) -> Any:
            if isinstance(value, str):
                return self._redact_text(value)
            if isinstance(value, dict):
                return {_walk(k): _walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_walk(v) for v in value]
            return value

        redacted = _walk(manifest)
        after = yaml.dump(redacted, allow_unicode=True)
        target = self._target(
            "capsule-yaml", "capsule.yaml",
            bytes_scanned=len(before.encode()),
            findings_count=len(findings),
            hash_before=_sha256(before.encode()),
            hash_after=_sha256(after.encode()),
        )
        return redacted, target, findings

    def assert_manifest_clean(self, manifest: dict[str, Any]) -> str:
        """Last gate before ``capsule.yaml`` is written and sealed; returns its YAML.

        The manifest is redacted by :meth:`redact_manifest` before it is first
        written, but the final manifest gains ``evidence_digests`` afterwards -- a
        map keyed by capsule file paths. This checks the *final* manifest twice:
        every string key and value on its own (YAML line-folding can split a long
        scalar, so the serialized text alone is not enough), and the exact YAML
        text that will be written. Any match raises :class:`ResidualSecretError`;
        the caller must not seal. Returns the checked text so the bytes written
        are the bytes checked.
        """
        import yaml

        hits: list[str] = []

        def _walk(value: Any) -> None:
            if isinstance(value, str):
                hits.extend(scan_text_rule_ids(value))
            elif isinstance(value, dict):
                for k, v in value.items():
                    _walk(k)
                    _walk(v)
            elif isinstance(value, list):
                for v in value:
                    _walk(v)

        _walk(manifest)
        text = yaml.dump(manifest, allow_unicode=True)
        hits.extend(scan_text_rule_ids(text))
        if hits:
            raise ResidualSecretError("capsule.yaml", hits)
        return text

    def residual_scan(self, proof: dict[str, Any]) -> dict[str, Any]:
        """Rescan every file of the finished capsule and fold the result into ``proof``.

        ADR-0009 promises that every byte written to the capsule is scanned. The
        main pass runs before late files exist (``lineage.jsonl``, ``replay.yaml``,
        the C2PA marker) and before ADR-0135 maskers rewrite files, so this pass
        runs last -- after every write, before the proof is written and before
        ``evidence_digests`` bind the bytes. Semantics are ADR-0009's own:
        redact-and-record. A residual in a text file is redacted in place; a
        binary carrying one is dropped; each residual is an ordinary finding
        (``match_hash`` unchanged) against the file's existing target.

        Reconciliation keeps the proof truthful about the final bytes:

        * an existing target keeps ``hash_before_redaction`` (the original
          bytes) and gets ``hash_after_redaction`` = the bytes now on disk;
        * a file no target names yet (e.g. ``replay.yaml``) gets a target of
          its own -- kind :data:`OTHER_FILE_KIND` unless a list names it;
        * ``residual_check`` (additive, optional) records the pass itself.

        Excluded: ``capsule.yaml`` (see :meth:`assert_manifest_clean`), the
        proof itself, and ``.seal/``. Returns a new, re-chained proof.
        """
        targets = [dict(t) for t in proof.get("targets", [])]
        by_ref = {t["ref"]: t for t in targets}
        new_findings: list[dict[str, Any]] = []
        residual_refs: list[str] = []
        reconciled_refs: list[str] = []
        files = rescanned_bytes = removed = 0

        for path, ref, kind, structured in list(self._all_capsule_files()):
            if structured:
                target, found, gone = self._scan_structured(path, ref, kind)
            else:
                target, found, gone = self._scan_artifact(path, ref, kind)
            files += 1
            rescanned_bytes += int(target["bytes_scanned"])
            removed += gone
            new_findings.extend(found)
            if found:
                residual_refs.append(str(target["ref"]))
            if _fold_target(targets, by_ref, target, len(found)):
                reconciled_refs.append(str(target["ref"]))

        if residual_refs:
            logger.warning(
                "novafabric.secrets: residual pass redacted %d match(es) the earlier "
                "passes missed, in %s",
                len(new_findings), ", ".join(residual_refs),
            )
        out = dict(proof)
        all_findings = list(proof.get("findings", [])) + new_findings
        out["targets"] = targets
        out["findings"] = all_findings
        out["findings_count"] = {
            "total": len(all_findings), "by_severity": _count_by_severity(all_findings)
        }
        out["bytes_scanned"] = sum(int(t["bytes_scanned"]) for t in targets)
        out["bytes_redacted"] = int(proof.get("bytes_redacted", 0)) + max(0, removed)
        out.pop("chain_hash", None)
        # The proof is itself written into the capsule. Its free-text fields --
        # an ADR-0135 masker's `replacement`, a ref -- come from code that is not
        # the scanner, so they get the same rules. Digests, ULIDs and counts cannot
        # match (the bare-hex/alnum rules are bounded and `sha256:`-guarded).
        out, scrubbed = _redact_strings(out)
        if scrubbed:
            logger.warning(
                "novafabric.secrets: %d string(s) in redaction-proof.json matched a "
                "rule and were masked before it was written",
                scrubbed,
            )
        out["residual_check"] = {
            "files_rescanned": files,
            "bytes_rescanned": rescanned_bytes,
            "residual_findings": len(new_findings),
            "residual_refs": residual_refs,
            "reconciled_refs": reconciled_refs,
            "proof_strings_redacted": scrubbed,
        }
        return recompute_chain_hash(out)

    def fold_rescan(
        self,
        proof: dict[str, Any],
        target: dict[str, Any],
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Fold a second scan of an already-recorded target into ``proof``.

        Unlike :func:`merge_scan_results`, which *replaces* a target, this keeps the
        first scan's ``hash_before_redaction`` and findings, adds the new findings,
        and moves ``hash_after_redaction`` to the rescanned bytes. Used for the
        manifest after ADR-0135 maskers ran over it. Returns a re-chained proof.
        """
        out = dict(proof)
        targets = [dict(t) for t in proof.get("targets", [])]
        by_ref = {t["ref"]: t for t in targets}
        _fold_target(targets, by_ref, target, len(findings))
        all_findings = list(proof.get("findings", [])) + list(findings)
        out["targets"] = targets
        out["findings"] = all_findings
        out["findings_count"] = {
            "total": len(all_findings), "by_severity": _count_by_severity(all_findings)
        }
        out["bytes_scanned"] = sum(int(t["bytes_scanned"]) for t in targets)
        return recompute_chain_hash(out)

    def scan_and_redact(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        findings: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        total_bytes_scanned = 0
        total_bytes_redacted = 0

        for filename, kind in _SCAN_TARGETS:
            path = self._dir / filename
            if not os.path.lexists(path):
                continue
            target, file_findings, removed = self._scan_structured(path, filename, kind)
            targets.append(target)
            findings.extend(file_findings)
            total_bytes_scanned += target["bytes_scanned"]
            total_bytes_redacted += removed

        for path, ref, kind in list(self._artifact_targets()):
            target, file_findings, removed = self._scan_artifact(path, ref, kind)
            targets.append(target)
            findings.extend(file_findings)
            total_bytes_scanned += target["bytes_scanned"]
            total_bytes_redacted += removed

        by_severity = _count_by_severity(findings)

        proof: dict[str, Any] = {
            "schema_version": "0.1.0",
            "proof_id": new_ulid(),
            "capsule_run_id": self._run_id,
            "created_at": now,
            "scanner": {
                "name": "novafabric.secrets",
                # 0.4.0: file-name check, residual pass (`residual_check`).
                "version": "0.4.0",
                "engine": "regex",
                "engine_version": "0.2.0",
            },
            "packs": [{
                "name": PACK_NAME,
                "version": PACK_VERSION,
                "rules_count": len(_RULES),
                "rules_hash": _PACK_RULES_HASH,
            }],
            "targets": targets,
            "findings_count": {"total": len(findings), "by_severity": by_severity},
            "findings": findings,
            "bytes_scanned": total_bytes_scanned,
            "bytes_redacted": max(0, total_bytes_redacted),
        }
        if self._overrides:
            proof["redaction_strategy_overrides"] = [
                {
                    "rule_id": rid,
                    "strategy": strat,
                    "rationale": "applied via --strategy-override",
                }
                for rid, strat in sorted(self._overrides.items())
            ]
        return recompute_chain_hash(proof)
