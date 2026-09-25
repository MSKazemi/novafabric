"""Session capsule (ADR-0122, experimental): group N independent runs.

A *session* is a content-addressed ``session.json`` manifest that references
N otherwise-independent Run Capsules in temporal order — one multi-turn
conversation or workflow. It copies no capsule data and preserves the
one-writer-per-capsule invariant: the session layer only reads and
references. A rebuildable SQLite index speeds up enumeration (P3) and a
portable, verifiable ZIP bundle carries a session with its members (P4).

Not the parent/child hierarchy (ADR-0032/0039), which groups the workers of
*one* distributed job. Sessions compose over parent/child: a session member
may itself be a distributed-run PARENT capsule.
"""

from novafabric.session.bundle import (
    BundleLimits,
    SessionBundleError,
    SessionBundleExport,
    SessionBundleImport,
    SessionBundleVerification,
    UnsafeBundleMemberError,
    export_session_bundle,
    import_session_bundle,
    verify_session_bundle,
)
from novafabric.session.index import (
    SESSION_INDEX_FILENAME,
    RebuildReport,
    SessionIndexError,
    SessionListing,
    list_sessions_fast,
    rebuild_index,
)
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
    SessionReplayPlan,
    SessionReplayRangeError,
    SessionReplayResult,
    TurnReplayPlan,
    TurnReplayResult,
    plan_session_replay,
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
    "SESSION_INDEX_FILENAME",
    "SESSION_MANIFEST_FILENAME",
    "SESSION_REPLAY_SCHEMA_VERSION",
    "BundleLimits",
    "DivergencePolicy",
    "DuplicateMemberError",
    "MemberRun",
    "MemberStatus",
    "NotACapsuleError",
    "RebuildReport",
    "ResolvedMember",
    "SessionBundleError",
    "SessionBundleExport",
    "SessionBundleImport",
    "SessionBundleVerification",
    "SessionError",
    "SessionFinalizedError",
    "SessionIndexError",
    "SessionIntegrityError",
    "SessionListing",
    "SessionManifest",
    "SessionNotFoundError",
    "SessionReplayError",
    "SessionReplayMode",
    "SessionReplayPlan",
    "SessionReplayRangeError",
    "SessionReplayResult",
    "SessionStats",
    "TurnReplayPlan",
    "TurnReplayResult",
    "UnsafeBundleMemberError",
    "add_member",
    "capsule_manifest_digest",
    "export_session_bundle",
    "import_session_bundle",
    "list_sessions",
    "list_sessions_fast",
    "load_session",
    "new_session",
    "plan_session_replay",
    "rebuild_index",
    "replay_session",
    "resolve_members",
    "save_session",
    "session_manifest_path",
    "session_stats",
    "sessions_root",
    "verify_session_bundle",
    "write_session_replay_result",
]
