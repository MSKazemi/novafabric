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

A mapping option also accepts a **JSON object** string, ``'{"K":"V"}'``: ADR-0270's
migration note and the v0.102.0 upgrade notes tell users to pass
``extra_env='{"LOG_LEVEL":"debug"}'``, and the ``k=v`` parser silently skipped it
(no ``=`` in ``{"K":"V"}``). A string that starts with ``{`` is that form and nothing
else, so it is parsed strictly: a malformed one raises :class:`RunnerOptionError`
rather than being dropped.
"""

from __future__ import annotations

import json
from typing import Any


class RunnerOptionError(ValueError):
    """A runner option was given in a recognised form but is malformed.

    A ``ValueError`` so the runners that already turn a bad ``extra_env`` into
    ``failed_setup`` (``shell_safe_env``) handle it the same way. The message names
    the problem and the offending key, never a value: ``extra_env`` values may be
    credentials.
    """


class _DuplicateKey(Exception):
    def __init__(self, key: str) -> None:
        self.key = key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _parse_json_object(text: str, name: str) -> dict[str, str]:
    """``'{"K":"V",...}'`` as a dict of non-empty string keys to string values."""
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey as dup:
        raise RunnerOptionError(f"{name} names {dup.key!r} more than once") from None
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError's message carries a position, not the text; RecursionError
        # is CPython's answer to pathological nesting.
        reason = exc.msg if isinstance(exc, json.JSONDecodeError) else "nested too deeply"
        raise RunnerOptionError(f"{name} is not valid JSON ({reason})") from None
    if not isinstance(obj, dict):  # pragma: no cover - a "{" prefix guarantees an object
        raise RunnerOptionError(f"{name} must be a JSON object")
    out: dict[str, str] = {}
    for key, value in obj.items():
        if not key:
            raise RunnerOptionError(f"{name} has an empty name")
        if not isinstance(value, str):
            raise RunnerOptionError(
                f"{name}: the value of {key!r} must be a string, got {type(value).__name__}"
            )
        out[key] = value
    return out


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


def coerce_str_dict(value: Any, *, name: str = "runner option") -> dict[str, str]:
    """A dict of strings from a real mapping, ``"k=v,k2=v2"`` or ``'{"k":"v"}'``.

    Only the **first** ``=`` separates key from value, so a value may itself
    contain ``=`` (base64 padding, query strings). An item with no ``=`` is
    skipped rather than guessed at.

    A string whose first non-blank character is ``{`` is a JSON object of string
    to string (commas and ``=`` inside its values are then literal). ``name`` is
    the option's name, used only in an error message.

    Raises:
        RunnerOptionError: the JSON form is malformed — invalid JSON, a duplicated
            or empty name, or a value that is not a string.
    """
    if isinstance(value, str):
        if value.lstrip().startswith("{"):
            return _parse_json_object(value, name)
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


def directive_value(name: str, value: str, *, allow_spaces: bool = False) -> str:
    """*value* as it may appear on a ``#BSUB`` / ``#PBS`` directive line.

    Directive lines are parsed by the scheduler, not by the shell, so they cannot
    be shell-quoted (review capture-runners-23): a line break would start another
    directive, and whitespace ends the value. Both are refused with a
    :class:`ValueError` naming the option. With *allow_spaces* (LSF ``-R``, whose
    resource strings contain spaces) the value is wrapped in double quotes, which
    then must not occur in it.
    """
    if any(c in value for c in "\r\n\0"):
        raise ValueError(
            f"runner option {name!r} contains a line break, which would add a scheduler "
            "directive; refused"
        )
    if allow_spaces:
        if '"' in value:
            raise ValueError(f"runner option {name!r} contains a double quote; refused")
        return f'"{value}"' if any(c.isspace() for c in value) else value
    if any(c.isspace() for c in value):
        raise ValueError(
            f"runner option {name!r} ({value!r}) contains whitespace, which a scheduler "
            "directive line cannot carry; use a value without it (for output_dir, which "
            "defaults to the capsule directory, a path without spaces)"
        )
    return value
