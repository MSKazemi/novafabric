"""Offline JSON Schema validation: a ``$ref`` is never retrieved.

``jsonschema`` (>= 4.18) resolves a ``$ref`` it cannot find in the schema
document through a deprecated default registry that hands the URI to
``urllib.request.urlopen``. For any schema NovaFabric did not ship — a tool's
``arguments_schema_ref`` / ``result_schema_ref``, a ``nova eval offline``
contract — that is an egress and file-read primitive driven by data: a nested
``{"$ref": "http://host/x.json"}`` issues an HTTP GET during capture (at every
capture level, ``air_gapped`` included), and ``file:///…`` reads a local file.

Every validator in this package is therefore built by :func:`offline_validator`,
whose registry *refuses* retrieval. What still resolves, with no I/O:

- refs inside the schema document (``#/$defs/x``, anchors, and subschemas
  embedded under their own ``$id``);
- the JSON Schema meta-schemas bundled with ``jsonschema``.

Anything else raises :class:`SchemaRefUnresolved` when validation reaches the
ref. Callers decide how to degrade: the capture-time validator records a
``schema-unresolved`` verdict of ``null``; a CLI reports a usage error.
"""
from __future__ import annotations

from typing import Any

import jsonschema  # type: ignore[import-untyped]
import referencing
import referencing.exceptions

__all__ = ["SchemaRefUnresolved", "offline_validator"]

#: Raised (lazily, by ``iter_errors`` / ``is_valid`` / ``validate``) when a
#: ``$ref`` points outside the schema document and the bundled meta-schemas.
#: jsonschema surfaces the refusal as a subclass of this ``referencing`` type;
#: a dangling local pointer (``#/$defs/missing``) is reported the same way.
SchemaRefUnresolved = referencing.exceptions.Unresolvable


def _refuse_retrieval(uri: str) -> referencing.Resource[Any]:
    """The registry's ``retrieve`` hook: never fetch, never open, never read."""
    raise referencing.exceptions.NoSuchResource(ref=uri)  # type: ignore[call-arg]  # attrs init


#: Retrieval-refusing registry. jsonschema combines any registry passed to a
#: validator with its bundled meta-schemas, so those stay resolvable offline.
_OFFLINE_REGISTRY: referencing.Registry[Any] = referencing.Registry(
    retrieve=_refuse_retrieval,  # type: ignore[call-arg]  # attrs init
)


def offline_validator(schema: Any, *, format_checker: Any = None) -> Any:
    """Return a Draft 2020-12 validator for *schema* that never retrieves a ``$ref``.

    *format_checker* is passed through unchanged (``None`` keeps ``format`` an
    annotation, as ``Draft202012Validator`` does by default). Construction does
    no I/O and does not raise for a non-local ref; reaching one during
    validation raises :class:`SchemaRefUnresolved`.
    """
    return jsonschema.Draft202012Validator(
        schema, format_checker=format_checker, registry=_OFFLINE_REGISTRY
    )
