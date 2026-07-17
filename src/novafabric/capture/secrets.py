from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture._ulid import new_ulid

PACK_NAME = "gitleaks-core-v0"
PACK_VERSION = "0.3.0"

# 13 key patterns — ordered from most to least specific to avoid false positives
_RULES: list[dict[str, Any]] = [
    # ADR-0193: our own credential format (`nvfk_<key_id>_<secret>`) — detect a
    # leaked NovaFabric API key in a capsule before anyone else does.
    {"id": "novafabric-api-key", "severity": "critical",
     "pattern": re.compile(r"nvfk_[A-Za-z0-9\-_]{8}_[A-Za-z0-9\-_]{30,60}")},
    {"id": "anthropic-api-key", "severity": "critical",
     "pattern": re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,80}")},
    {"id": "openai-api-key", "severity": "critical",
     "pattern": re.compile(r"sk-(?!ant-)[A-Za-z0-9]{20,60}")},
    {"id": "huggingface-token", "severity": "high",
     "pattern": re.compile(r"hf_[A-Za-z0-9]{34,50}")},
    {"id": "replicate-api-key", "severity": "high",
     "pattern": re.compile(r"r8_[A-Za-z0-9]{37}")},
    {"id": "langfuse-key", "severity": "medium",
     "pattern": re.compile(r"pk-lf-[A-Za-z0-9\-]{30,50}")},
    {"id": "langsmith-key", "severity": "medium",
     "pattern": re.compile(r"ls__[A-Za-z0-9]{40,60}")},
    {"id": "weaviate-api-key", "severity": "medium",
     "pattern": re.compile(r"wcs_[A-Za-z0-9]{30,50}")},
    {"id": "qdrant-api-key", "severity": "medium",
     "pattern": re.compile(r"qdrant_[A-Za-z0-9]{30,50}")},
    {"id": "cohere-api-key", "severity": "high",
     "pattern": re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{40}(?![A-Za-z0-9])")},
    # False-positive guard (pack 0.2.1, ADR-0125): a bare 64-hex string that is
    # a capsule content address — prefixed "sha256:" (Artifact/MediaPart
    # content_hash) or "outputs/" (content-addressed blob ref) — is NOT a
    # Together API key. Real keys never carry those prefixes.
    {"id": "together-api-key", "severity": "high",
     "pattern": re.compile(
         r"(?<![0-9a-f])(?<!sha256:)(?<!outputs/)[0-9a-f]{64}(?![0-9a-f])"
     )},
    {"id": "mistral-api-key", "severity": "high",
     "pattern": re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")},
    {"id": "pinecone-api-key", "severity": "medium",
     "pattern": re.compile(
         r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
     )},
]

_PACK_RULES_HASH = "sha256:" + hashlib.sha256(
    "|".join(str(r["id"]) for r in _RULES).encode()
).hexdigest()

_SCAN_TARGETS = [
    ("model-calls.jsonl", "model-call-messages"),
    ("tool-calls.jsonl", "tool-call-arguments"),
    ("trace.jsonl", "trace"),
    ("capsule.yaml", "capsule-yaml"),
]

# Public alias: the ADR-0135 masking pipeline walks exactly the same targets
# the built-in scanner does (design/spec/pii-masking-pipeline-v0.md).
SCAN_TARGETS: list[tuple[str, str]] = _SCAN_TARGETS


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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


def redact_secrets_in_text(text: str) -> str:
    """Mask every rule match in ``text`` — same pack the capsule scanner uses.

    Reused by the lifecycle-event emitter (ADR-0137 D5) as the
    scan-before-emit payload-hygiene pass. Always applies the ``mask``
    strategy; returns the redacted text.
    """
    for rule in _RULES:
        rule_id = str(rule["id"])
        text = rule["pattern"].sub(
            lambda m, rid=rule_id: _replacement(rid, "mask", m.group()),
            text,
        )
    return text


def recompute_chain_hash(proof: dict[str, Any]) -> dict[str, Any]:
    """Recompute chain_hash over a proof's canonical JSON. Returns the same dict."""
    proof = dict(proof)
    proof.pop("chain_hash", None)
    canonical = json.dumps(proof, sort_keys=True, separators=(",", ":"))
    proof["chain_hash"] = _sha256(canonical.encode())
    return proof


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

    def scan_and_redact(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        findings: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        total_bytes_scanned = 0
        total_bytes_redacted = 0

        for filename, kind in _SCAN_TARGETS:
            path = self._dir / filename
            if not path.exists():
                continue

            original = path.read_bytes()
            content = original.decode("utf-8", errors="replace")
            hash_before = _sha256(original)
            file_findings: list[dict[str, Any]] = []

            # Collect findings before substitution (offsets on original content)
            for rule in _RULES:
                rule_id = rule["id"]
                strategy = self._strategy_for(rule_id)
                for m in rule["pattern"].finditer(content):
                    matched = m.group()
                    file_findings.append({
                        "finding_id": new_ulid(),
                        "rule_id": rule_id,
                        "rule_version": "0.1.0",
                        "pack": PACK_NAME,
                        "severity": rule["severity"],
                        "target_kind": kind,
                        "target_ref": filename,
                        "byte_offset": m.start(),
                        "byte_length": len(matched.encode()),
                        "match_hash": _sha256(matched.encode()),
                        "redaction_strategy": strategy,
                        "replacement": _replacement(rule_id, strategy, matched),
                    })

            # Apply substitutions per rule, honoring strategy overrides
            redacted_content = content
            for rule in _RULES:
                rule_id = rule["id"]
                strategy = self._strategy_for(rule_id)
                redacted_content = rule["pattern"].sub(
                    lambda m, rid=rule_id, s=strategy: _replacement(rid, s, m.group()),
                    redacted_content,
                )

            redacted = redacted_content.encode("utf-8")
            hash_after = _sha256(redacted)

            if redacted != original:
                path.write_bytes(redacted)
                total_bytes_redacted += max(0, len(original) - len(redacted))

            total_bytes_scanned += len(original)
            findings.extend(file_findings)
            targets.append({
                "kind": kind,
                "ref": filename,
                "bytes_scanned": len(original),
                "findings_count": len(file_findings),
                "skipped": False,
                "binary": False,
                "hash_before_redaction": hash_before,
                "hash_after_redaction": hash_after,
            })

        by_severity: dict[str, int] = {
            "critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0
        }
        for f in findings:
            sev = str(f["severity"])
            if sev in by_severity:
                by_severity[sev] += 1

        proof: dict[str, Any] = {
            "schema_version": "0.1.0",
            "proof_id": new_ulid(),
            "capsule_run_id": self._run_id,
            "created_at": now,
            "scanner": {
                "name": "novafabric.secrets",
                "version": "0.2.0",
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
