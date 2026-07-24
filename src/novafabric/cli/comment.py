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

"""CLI for append-only capsule comments (experimental, ADR-0121)."""

from __future__ import annotations

import getpass
import json
from pathlib import Path

import typer

from novafabric.capsule.comments import (
    COMMENTS_FILENAME,
    Comment,
    CommentSecretError,
    CommentThreadCycleError,
    SubjectKind,
    append_comment,
    apply_tombstones,
    capsule_subject_digest,
    gate_comment_body,
    read_comments,
    resolve_thread,
)

app = typer.Typer(
    help=(
        "Append-only comments on capsule evidence — portable annotations, "
        "not live chat (experimental, ADR-0121)."
    ),
    no_args_is_help=True,
)

_SHA256_PREFIX = "sha256:"


def _default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no resolvable local user
        return "unknown"


def _resolve_subject(subject: str, capsule: Path | None) -> tuple[str, Path | None]:
    """Resolve a ``--subject`` ref to ``(sha256 digest, capsule directory)``.

    Accepts a capsule path (resolved to its stable root digest) or a
    ``sha256:`` digest (requires ``--capsule`` to locate ``comments.jsonl``).
    An ``asset://<type>/<name>@<version>`` ref is returned verbatim with no
    capsule directory: asset comments live in the registry's
    ``asset_comments`` table, not in a capsule log (ADR-0121 P3).
    """
    if subject.startswith("asset://"):
        return subject, None
    if subject.startswith(_SHA256_PREFIX):
        if capsule is None:
            typer.echo(
                "Error: --capsule <dir> is required when --subject is a sha256: digest "
                "(it locates the capsule's comments.jsonl).",
                err=True,
            )
            raise typer.Exit(code=2)
        return subject, capsule
    path = Path(subject)
    if not path.is_dir():
        typer.echo(
            f"Error: subject {subject!r} is neither a sha256: digest, an existing "
            "capsule directory, nor an asset:// ref.",
            err=True,
        )
        raise typer.Exit(code=2)
    return capsule_subject_digest(path), path


def _load_comments(digest: str, capsule_dir: Path | None) -> list[Comment]:
    """Read a subject's comments from whichever backend owns it.

    The two backends return the same `Comment` records, so every reader-side
    helper (apply_tombstones, resolve_thread) works on both unchanged.
    """
    if capsule_dir is None:  # asset:// — registry-backed (ADR-0121 P3)
        from novafabric.registry import asset_comments as _asset_comments
        from novafabric.registry.store import get_connection

        conn = get_connection(_asset_comments.default_db_path())
        try:
            return _asset_comments.read_comments(conn, digest)
        finally:
            conn.close()
    return read_comments(capsule_dir / COMMENTS_FILENAME)


_CAPSULE_OPT = typer.Option(
    None,
    "--capsule",
    exists=True,
    file_okay=False,
    dir_okay=True,
    resolve_path=True,
    help="Capsule directory holding comments.jsonl (required when --subject is a digest).",
)


@app.command("add")
def comment_add_cmd(
    subject: str = typer.Option(
        ...,
        "--subject",
        help="Annotated object: a capsule directory path or a sha256:<hex> digest.",
    ),
    body: str = typer.Option(..., "--body", help="Free-text comment body (secret-scanned)."),
    author: str | None = typer.Option(
        None, "--author", help="Author identity (default: local username)."
    ),
    kind: SubjectKind = typer.Option(
        SubjectKind.CAPSULE.value,
        "--kind",
        help="What the subject digest addresses: capsule | span | run | score.",
    ),
    reply_to: str | None = typer.Option(
        None, "--reply-to", help="comment_id this comment replies to (or supersedes)."
    ),
    tags: list[str] | None = typer.Option(
        None, "--tag", help="Optional label; repeatable."
    ),
    tombstone: bool = typer.Option(
        False,
        "--tombstone",
        help="Retract the comment named by --reply-to (append-only delete).",
    ),
    redact: bool = typer.Option(
        False,
        "--redact",
        help="Mask secret matches in the body instead of refusing the write.",
    ),
    capsule: Path | None = _CAPSULE_OPT,
    json_output: bool = typer.Option(
        False, "--json", help="Emit the stored record as JSON to stdout."
    ),
) -> None:
    """Append an immutable comment to a capsule's comments.jsonl (experimental).

    The body passes the ADR-0009 secret-scan gate before storage: by default a
    body that trips a secret pattern is refused (exit 3); --redact masks it
    instead. Comments are never edited or deleted in place — an edit is a new
    comment with --reply-to, a delete is --tombstone --reply-to <id>.

    Scope: single capsule.

    \b
    Examples:
      nova comment add --subject .novafabric/runs/01HX.../ --body "stale doc, blocking promotion"
      nova comment add --subject sha256:9f2c... --capsule runs/01HX.../ --kind span --body "..."
      nova comment add --subject runs/01HX.../ --reply-to 01HXB0K3M7QM4YZ2K7N9DPBYK2 --body "fixed"
      nova comment add --subject runs/01HX.../ --tombstone --reply-to 01HXB0K3... --body "oops"
    """
    if tombstone and reply_to is None:
        typer.echo("Error: --tombstone requires --reply-to <comment_id>.", err=True)
        raise typer.Exit(code=2)
    digest, capsule_dir = _resolve_subject(subject, capsule)

    try:
        stored_body, redaction_applied = gate_comment_body(body, redact=redact)
    except CommentSecretError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=3) from exc

    try:
        comment = Comment(
            subject=digest,
            subject_kind=kind,
            author=author or _default_author(),
            body=stored_body,
            in_reply_to=reply_to,
            tags=list(tags) if tags else None,
            tombstone=tombstone,
            redaction_applied=redaction_applied,
        )
    except ValueError as exc:
        typer.echo(f"Error: invalid comment: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if capsule_dir is None:  # asset:// subject — registry-backed (ADR-0121 P3)
        from novafabric.registry import asset_comments as _asset_comments
        from novafabric.registry.store import get_connection

        conn = get_connection(_asset_comments.default_db_path())
        try:
            _asset_comments.append_comment(conn, comment)
        finally:
            conn.close()
        target_desc = f"registry ({_asset_comments.TABLE})"
    else:
        append_comment(capsule_dir / COMMENTS_FILENAME, comment)
        target_desc = str(capsule_dir / COMMENTS_FILENAME)

    if json_output:
        typer.echo(comment.model_dump_json(exclude_none=True))
    else:
        note = " (body redacted)" if redaction_applied else ""
        typer.echo(f"✓ comment {comment.comment_id} appended to {target_desc}{note}")


@app.command("list")
def comment_list_cmd(
    subject: str = typer.Option(
        ...,
        "--subject",
        help="Subject to list: a capsule directory path or a sha256:<hex> digest.",
    ),
    capsule: Path | None = _CAPSULE_OPT,
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Show every raw record (any subject, including tombstones and retracted comments).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the comments as a JSON array to stdout."
    ),
) -> None:
    """List comments on a subject in write order (experimental).

    By default, tombstoned (retracted) comments and the tombstone markers are
    hidden — the bytes stay in comments.jsonl (append-only); use --all for the
    full audit trail.

    Scope: single capsule.

    \b
    Examples:
      nova comment list --subject .novafabric/runs/01HX.../
      nova comment list --subject sha256:9f2c... --capsule runs/01HX.../ --json
      nova comment list --subject runs/01HX.../ --all
    """
    digest, capsule_dir = _resolve_subject(subject, capsule)
    try:
        comments = _load_comments(digest, capsule_dir)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if show_all:
        selected = comments
    else:
        selected = [c for c in apply_tombstones(comments) if c.subject == digest]

    if json_output:
        typer.echo(
            json.dumps([json.loads(c.model_dump_json(exclude_none=True)) for c in selected])
        )
        return
    if not selected:
        typer.echo("(no comments)")
        return
    for c in selected:
        typer.echo(_format_comment(c))


def _format_comment(c: Comment, *, indent: int = 0) -> str:
    marks = ""
    if c.tombstone:
        marks += " [tombstone]"
    if c.redaction_applied:
        marks += " [redacted]"
    reply = f" ↩ {c.in_reply_to}" if c.in_reply_to else ""
    prefix = "  " * indent
    return f"{prefix}{c.comment_id}  {c.created_at}  {c.author}{reply}{marks}: {c.body}"


@app.command("thread")
def comment_thread_cmd(
    comment_id: str = typer.Argument(
        ..., help="Comment id whose reply chain should be resolved."
    ),
    subject: str = typer.Option(
        ...,
        "--subject",
        help="Subject holding the comment: a capsule directory path or a sha256:<hex> digest.",
    ),
    capsule: Path | None = _CAPSULE_OPT,
    json_output: bool = typer.Option(
        False, "--json", help="Emit the thread as a JSON array to stdout."
    ),
) -> None:
    """Resolve the reply chain containing a comment, root first (experimental).

    Walks `in_reply_to` links iteratively with a seen-set, so a malformed
    cycle is reported rather than looping forever, and a reply whose parent is
    missing is treated as an orphan root rather than an error — the append-only
    log can legitimately contain both.

    Scope: single capsule.

    \b
    Examples:
      nova comment thread cmt_01HX... --subject .novafabric/runs/01HX.../
      nova comment thread cmt_01HX... --subject runs/01HX.../ --json
    """
    digest, capsule_dir = _resolve_subject(subject, capsule)
    try:
        comments = _load_comments(digest, capsule_dir)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        chain = resolve_thread(comments, comment_id)
    except KeyError:
        typer.echo(f"Error: comment {comment_id!r} not found in {subject}", err=True)
        raise typer.Exit(code=1) from None
    except CommentThreadCycleError as exc:
        # Exit 1, not a traceback: a cycle is corrupt data in an append-only
        # log, which the operator needs told plainly.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps([json.loads(c.model_dump_json(exclude_none=True)) for c in chain])
        )
        return
    for depth, c in enumerate(chain):
        typer.echo(_format_comment(c, indent=depth))
