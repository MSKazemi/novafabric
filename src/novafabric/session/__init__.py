"""Session capsule (ADR-0122, experimental): group N independent runs.

A *session* is a content-addressed ``session.json`` manifest that references
N otherwise-independent Run Capsules in temporal order — one multi-turn
conversation or workflow. It copies no capsule data and preserves the
one-writer-per-capsule invariant: the session layer only reads and
references.

Not the parent/child hierarchy (ADR-0032/0039), which groups the workers of
*one* distributed job. Sessions compose over parent/child: a session member
may itself be a distributed-run PARENT capsule.
"""

from novafabric.session.manifest import (
    SESSION_MANIFEST_FILENAME,
    DuplicateMemberError,
    MemberRun,
    NotACapsuleError,
    SessionError,
    SessionFinalizedError,
    SessionIntegrityError,
    SessionManifest,
    SessionNotFoundError,
    add_member,
    capsule_manifest_digest,
    list_sessions,
    load_session,
    new_session,
    save_session,
    session_manifest_path,
    sessions_root,
)
from novafabric.session.replay import (
    SESSION_REPLAY_SCHEMA_VERSION,
    DivergencePolicy,
    SessionReplayError,
    SessionReplayMode,
    SessionReplayResult,
    TurnReplayResult,
    replay_session,
    write_session_replay_result,
)
from novafabric.session.view import (
    MemberStatus,
    ResolvedMember,
    SessionStats,
    resolve_members,
    session_stats,
)

__all__ = [
    "SESSION_MANIFEST_FILENAME",
    "SESSION_REPLAY_SCHEMA_VERSION",
    "DivergencePolicy",
    "DuplicateMemberError",
    "MemberRun",
    "MemberStatus",
    "NotACapsuleError",
    "ResolvedMember",
    "SessionError",
    "SessionFinalizedError",
    "SessionIntegrityError",
    "SessionManifest",
    "SessionNotFoundError",
    "SessionReplayError",
    "SessionReplayMode",
    "SessionReplayResult",
    "SessionStats",
    "TurnReplayResult",
    "add_member",
    "capsule_manifest_digest",
    "list_sessions",
    "load_session",
    "new_session",
    "replay_session",
    "resolve_members",
    "save_session",
    "session_manifest_path",
    "session_stats",
    "sessions_root",
    "write_session_replay_result",
]
