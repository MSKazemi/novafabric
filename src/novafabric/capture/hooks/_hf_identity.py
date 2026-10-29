"""What a Hugging Face wire record can honestly say about *which model* ran (HF-02).

The wire hook sees the request, not the response. For a Hugging Face call that leaves three
different questions with three different answers, and the record must not blur them:

* **requested model** -- the ``model`` the caller sent, minus any routing selector. Observed.
* **requested selector** -- the suffix the router accepts on a model id: a policy
  (``:fastest``, ``:cheapest``, ``:preferred``) or an explicit provider (``:groq``). What the
  caller *asked for*, not what happened. (Documented at
  https://huggingface.co/docs/inference-providers/index, checked 2026-10-28.)
* **reported model / served provider** -- what the response says ran. The router selects the
  provider server-side by default and the docs promise no field that discloses it, and the
  wire hook never reads the response, so both are ``None`` ("not observed"), never guessed.

``gen_ai.response.model`` on every wire record mirrors the request (see
``extract_request_attributes``); for Hugging Face records read this block instead.
"""
from __future__ import annotations

import re
from typing import Any

IDENTITY_EXT = "io.novafabric.model_identity"
HF_SYSTEM_PREFIX = "huggingface"
_POLICIES = frozenset({"fastest", "cheapest", "preferred"})
#: Provider names the router accepts after ``:``. Any other tail (``:latest``, ``:8b``, ``:fp8``)
#: is part of the model string as sent, not a provider choice, and is left unsplit.
_PROVIDERS = frozenset({
    "auto", "black-forest-labs", "cerebras", "cohere", "fal-ai", "featherless-ai", "fireworks-ai",
    "groq", "hf-inference", "hyperbolic", "nebius", "novita", "nscale", "ovhcloud", "publicai",
    "replicate", "sambanova", "scaleway", "together", "wavespeed", "zai-org",
})
_SELECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")
_MAX_MODEL_LEN = 256


def split_selector(model: str) -> tuple[str, str | None, str | None]:
    """``(model, selector, kind)`` where kind is ``policy``, ``explicit_provider`` or ``None``."""
    base, sep, tail = model.rpartition(":")
    if not sep or not base or "/" in tail or "://" in model or not _SELECTOR_RE.match(tail):
        return model, None, None
    if tail not in _POLICIES and tail not in _PROVIDERS:
        return model, None, None
    return base, tail, "policy" if tail in _POLICIES else "explicit_provider"


def identity_extension(gen_ai_system: str, model: Any) -> dict[str, Any] | None:
    """The identity block for a Hugging Face record, or ``None`` for any other system."""
    if not isinstance(gen_ai_system, str) or not gen_ai_system.startswith(HF_SYSTEM_PREFIX):
        return None
    text = model if isinstance(model, str) and model != "unknown" else None
    if text is not None:
        text = text[:_MAX_MODEL_LEN]
    # Only the router has ``:policy`` / ``:provider`` selectors; a dedicated endpoint does not.
    base: str | None = text
    selector: str | None = None
    kind: str | None = None
    if text and gen_ai_system == "huggingface":
        base, selector, kind = split_selector(text)
    return {
        "requested_model": base,
        "requested_selector": selector,
        "selector_kind": kind,
        "reported_model": None,
        "served_provider": None,
        "identity_source": "request_only",
    }
