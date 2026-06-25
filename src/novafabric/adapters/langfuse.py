from __future__ import annotations

import os


def resolve_prompt_uri(uri: str) -> str | None:
    if not os.environ.get("LANGFUSE_HOST"):
        return None
    return uri
