# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Coercion for ``runner_options`` values, which arrive in two shapes.

A runner option can reach a runner by two routes, and they do **not** produce
the same Python types:

- from a config file, where YAML gives real lists and dicts; and
- from ``nova capture --runner-option key=value``, where
  :func:`novafabric.cli.capture._parse_runner_option` can only ever produce a
  **string** — it partitions on the first ``=`` and stores the remainder.

Every structured option was typed for the first route only (``if not
isinstance(value, list): return []``), so from the command line ``extra_volumes``,
``extra_env`` and ``node_selector`` were accepted by the parser, passed to the
runner, and then **silently discarded**: no error, no warning, and a container
that simply did not have the mount the user asked for.

These helpers accept both routes. The string forms are the conventional ones for
a CLI — comma-separated items, ``key=value`` pairs — and are deliberately narrow:
anything that is neither a string nor the structured type still yields empty,
because guessing at a malformed option is worse than ignoring it.
"""

from __future__ import annotations

from typing import Any


def coerce_str_list(value: Any) -> list[str]:
    """A list of strings from either a real list or a comma-separated string.

    ``-v`` volume specs (``host:container[:opts]``) never contain a comma, so
    splitting on it is unambiguous for the options this serves. Empty items are
    dropped, so a trailing comma is not an error.
    """
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def coerce_str_dict(value: Any) -> dict[str, str]:
    """A dict of strings from either a real mapping or ``"k=v,k2=v2"``.

    Only the **first** ``=`` separates key from value, so a value may itself
    contain ``=`` (base64 padding, query strings). An item with no ``=`` is
    skipped rather than guessed at.
    """
    if isinstance(value, str):
        out: dict[str, str] = {}
        for item in value.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, _, val = item.partition("=")
            key = key.strip()
            if key:
                out[key] = val.strip()
        return out
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}
