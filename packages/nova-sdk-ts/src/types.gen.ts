/**
 * This file was auto-generated from api/openapi.yaml by
 * scripts/generate-types.mjs (openapi-typescript). Do not edit by hand —
 * run `npm run generate:types` instead. Drift is gated by
 * `npm run check:drift`.
 */
export interface paths {
    "/assets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List registered assets */
        get: operations["listAssets"];
        put?: never;
        /** Register a new asset from a YAML spec */
        post: operations["createAsset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/assets/{id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get a single asset by ID */
        get: operations["getAsset"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/assets/{id}/promote": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Promote an asset to a new lifecycle status */
        put: operations["promoteAsset"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/capsules": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List run capsules */
        get: operations["listCapsules"];
        put?: never;
        /** Upload a capsule (multipart/form-data ZIP) */
        post: operations["uploadCapsule"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/capsules/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get capsule detail by run ID */
        get: operations["getCapsule"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/capsules/{run_id}/scores": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Submit an externally-computed score into the capsule's append-only scores.jsonl (ADR-0119, experimental)
         * @description Writer-role required (the scores:write capability). Fail-closed: a rejection writes nothing. Append-only: a correction is a new record whose `supersedes` names a prior score_id; no line is ever mutated. Idempotent by score_id (identical replay returns 200 and appends no second line).
         */
        post: operations["submitCapsuleScore"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/lineage/nodes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List lineage nodes */
        get: operations["listLineageNodes"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/lineage/blast-radius": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Compute blast radius (downstream dependents) from a node reference */
        get: operations["lineageBlastRadius"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/lineage/provenance": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Compute provenance (upstream ancestors) from a node reference */
        get: operations["lineageProvenance"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/lineage/replay-chain": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get the replay chain for a run */
        get: operations["lineageReplayChain"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/replays": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Schedule a replay job */
        post: operations["scheduleReplay"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/replays/{replay_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get replay job status */
        get: operations["getReplay"];
        put?: never;
        post?: never;
        /** Cancel a pending or running replay job */
        delete: operations["cancelReplay"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/replays/{replay_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Stream replay progress as Server-Sent Events */
        get: operations["replayEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/evidence": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Build an evidence bundle ZIP from a capsule */
        post: operations["exportEvidence"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/evidence/{bundle_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get evidence bundle metadata */
        get: operations["getEvidenceBundle"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/evidence/{bundle_id}/download": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Download an evidence bundle ZIP */
        get: operations["downloadEvidenceBundle"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/device/code": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Issue a device code (Device Authorization Grant)
         * @description Initiates the Device Authorization Grant flow (RFC 8628).
         *     Returns a device_code and user_code; the client polls /auth/token
         *     until the user approves at the verification_uri.
         */
        post: operations["issueDeviceCode"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/token": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Poll for a token after device code approval
         * @description Client polls this endpoint until the user approves the device code.
         *     Returns the access token on approval, or a pending/expired error.
         */
        post: operations["pollDeviceToken"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/approve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Approve a pending device code (admin / testing)
         * @description Admin endpoint that approves a pending device code, associating a
         *     subject and roles with it. Used for testing or headless admin flows.
         */
        post: operations["approveDeviceCode"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/admin/flush-jwks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Flush the in-memory JWKS cache
         * @description Invalidates the cached JWKS so the next request re-fetches from the
         *     OIDC issuer. Requires admin role. Use after rotating signing keys at
         *     the identity provider.
         */
        post: operations["flushJwksCache"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/lineage/shard-local-query": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Execute a shard-local lineage query
         * @description Accepts an authenticated query from the federation coordinator. Returns per-site result rows only — never raw edge tuples (FR-10 sovereignty).
         */
        post: operations["shardLocalQuery"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/federation/query": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Fan-out cross-site lineage query
         * @description Stateless federation coordinator: fans out to all participating sites, deduplicates on node_id (ULID), returns merged result set. Marks partial=true if any site is offline.
         */
        post: operations["federationQuery"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/federation/summary": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Cross-site reference summary
         * @description Returns rolled-up per-site cross-site reference counts by (edge_type, target_site), tagged with source=summary and refresh timestamp.
         */
        get: operations["getFederationSummary"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api-keys": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List API keys (metadata only)
         * @description Returns key metadata only — never secrets or hashes. Requires admin role. Each row carries a derived status (active/revoked/expired).
         */
        get: operations["listApiKeys"];
        put?: never;
        /**
         * Create an API key
         * @description Creates a first-class API key bound to an owning principal, a role set (drawn from the existing RBAC vocabulary), an optional workspace scope, and an optional expiry. The full key string is returned exactly ONCE in the response and is unrecoverable thereafter — only its sha256 is stored. Requires admin role.
         */
        post: operations["createApiKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api-keys/{key_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /**
         * Revoke an API key
         * @description Revokes the key immediately — verification is a DB lookup, so the key is rejected on the very next request. Requires admin role.
         */
        delete: operations["revokeApiKey"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api-keys/{key_id}/rotate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Rotate an API key
         * @description Mints a successor key with identical bindings (owner, roles, workspace, expiry). Both keys stay valid for a bounded overlap window; after it elapses the predecessor auto-revokes at verify time (no background job). The successor's full key is returned exactly ONCE. Requires admin role.
         */
        post: operations["rotateApiKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        ErrorDetail: {
            /** @example not_found */
            code: string;
            /** @example Asset 'my-agent@v1.0' not found. */
            message: string;
            details?: {
                [key: string]: unknown;
            };
        };
        ErrorEnvelope: {
            error: components["schemas"]["ErrorDetail"];
        };
        /** @description Secret-free API-key metadata. Never contains the secret or its hash. */
        ApiKeyMetadata: {
            /** @description Public 8-char identifier, safe in logs and listings. */
            key_id: string;
            owner: string;
            roles: ("reader" | "writer" | "admin" | "auditor")[];
            workspace?: string | null;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            expires_at?: string | null;
            /**
             * Format: date-time
             * @description Coarse last-used timestamp (at most one write per interval).
             */
            last_used_at?: string | null;
            /** Format: date-time */
            revoked_at?: string | null;
            /**
             * @description Derived on read; present on list responses.
             * @enum {string}
             */
            status?: "active" | "revoked" | "expired";
        };
        PaginationMeta: {
            /** @description Opaque cursor for the next page; null when no more pages. */
            next_cursor?: string | null;
            /** @description Total count of matching items (without pagination applied). */
            total: number;
        };
        /** @enum {string} */
        AssetStatus: "development" | "staging" | "production" | "archived";
        /** @enum {string} */
        AssetType: "model" | "agent" | "prompt" | "tool" | "dataset" | "evaluation" | "deployment";
        AssetSummary: {
            /** Format: uuid */
            id: string;
            name: string;
            /** @description SemVer string. */
            version: string;
            asset_type: components["schemas"]["AssetType"];
            status: components["schemas"]["AssetStatus"];
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            promoted_at?: string | null;
            git_commit_sha?: string | null;
        };
        AssetDetail: components["schemas"]["AssetSummary"] & {
            /** @description Raw JSON-serialized asset spec. */
            spec_json?: string;
            promoted_by?: string | null;
            forced_promotion?: boolean;
        };
        AssetCreateRequest: {
            /** @description Full YAML-formatted asset spec (novafabric_spec_version required). */
            spec_yaml: string;
        };
        PromoteRequest: {
            to_status: components["schemas"]["AssetStatus"];
            /** @default api */
            actor: string;
            /**
             * @description Skip the evaluation gate for agents (recorded as forced_promotion).
             * @default false
             */
            force: boolean;
        };
        AssetListResponse: components["schemas"]["PaginationMeta"] & {
            items: components["schemas"]["AssetSummary"][];
        };
        CapsuleSummary: {
            run_id: string;
            /** @enum {string} */
            status: "success" | "failure" | "unknown";
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            finished_at?: string | null;
            duration_ms?: number | null;
            command?: string[] | null;
            exit_code?: number | null;
        };
        CapsuleDetail: components["schemas"]["CapsuleSummary"] & {
            schema_version?: string;
            novafabric_version?: string;
            model_call_count?: number;
            tool_call_count?: number;
            mutating_tool_count?: number;
            capture_mode?: string;
        };
        CapsuleListResponse: components["schemas"]["PaginationMeta"] & {
            items: components["schemas"]["CapsuleSummary"][];
        };
        LineageNode: {
            node_id: string;
            /** @enum {string} */
            kind: "run" | "asset" | "artifact" | "model" | "tool";
            ref: string;
            payload?: {
                [key: string]: unknown;
            };
        };
        LineageNodeListResponse: components["schemas"]["PaginationMeta"] & {
            items: components["schemas"]["LineageNode"][];
        };
        LineageQueryResponse: {
            ref: string;
            kind?: string | null;
            depth: number;
            nodes: components["schemas"]["LineageNode"][];
        };
        ReplayChainResponse: {
            run_id: string;
            chain: components["schemas"]["LineageNode"][];
        };
        /** @enum {string} */
        ReplayMode: "forensic" | "mocked" | "shadow" | "dry_run";
        ReplayScheduleRequest: {
            /** @description The capsule run_id to replay. */
            run_id: string;
            mode: components["schemas"]["ReplayMode"];
        };
        /** @enum {string} */
        ReplayStatus: "pending" | "running" | "completed" | "failed" | "cancelled";
        ReplaySummary: {
            replay_id: string;
            run_id: string;
            mode: components["schemas"]["ReplayMode"];
            status: components["schemas"]["ReplayStatus"];
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            finished_at?: string | null;
            error?: string | null;
        };
        EvidenceExportRequest: {
            /** @description The capsule run_id to export as evidence. */
            run_id: string;
            /** @description Optional override for the output ZIP path (server-side path). */
            output_path?: string | null;
            /** @default false */
            allow_unsafe_skips: boolean;
        };
        BundleSummary: {
            bundle_id: string;
            run_id: string;
            /** Format: date-time */
            created_at: string;
            size_bytes: number;
            bundle_path?: string | null;
        };
    };
    responses: {
        /** @description Invalid request parameters or body. */
        BadRequest: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description Missing or invalid bearer credentials. */
        Unauthorized: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description Authenticated but not permitted (RBAC role insufficient). */
        Forbidden: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description The requested resource was not found. */
        NotFound: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description Resource conflict (e.g. duplicate asset or invalid lifecycle transition). */
        Conflict: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description Spec validation error. */
        UnprocessableEntity: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description Promotion gate blocked (agent missing passing eval). */
        PreconditionFailed: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
        /** @description Unexpected server error. */
        InternalError: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/json": components["schemas"]["ErrorEnvelope"];
            };
        };
    };
    parameters: {
        /** @description Maximum number of items to return (default 50, max 500). */
        LimitParam: number;
        /** @description Opaque pagination cursor returned from a previous list call. */
        CursorParam: string;
    };
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    listAssets: {
        parameters: {
            query?: {
                /** @description Maximum number of items to return (default 50, max 500). */
                limit?: components["parameters"]["LimitParam"];
                /** @description Opaque pagination cursor returned from a previous list call. */
                cursor?: components["parameters"]["CursorParam"];
                asset_type?: components["schemas"]["AssetType"];
                status?: components["schemas"]["AssetStatus"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Paginated list of assets. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetListResponse"];
                };
            };
            400: components["responses"]["BadRequest"];
            500: components["responses"]["InternalError"];
        };
    };
    createAsset: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AssetCreateRequest"];
            };
        };
        responses: {
            /** @description Asset registered successfully. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetDetail"];
                };
            };
            400: components["responses"]["BadRequest"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["UnprocessableEntity"];
            500: components["responses"]["InternalError"];
        };
    };
    getAsset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description Asset UUID (from registration). */
                id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Asset detail. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetDetail"];
                };
            };
            404: components["responses"]["NotFound"];
            500: components["responses"]["InternalError"];
        };
    };
    promoteAsset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PromoteRequest"];
            };
        };
        responses: {
            /** @description Asset promoted successfully. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetDetail"];
                };
            };
            400: components["responses"]["BadRequest"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            412: components["responses"]["PreconditionFailed"];
            500: components["responses"]["InternalError"];
        };
    };
    listCapsules: {
        parameters: {
            query?: {
                /** @description Maximum number of items to return (default 50, max 500). */
                limit?: components["parameters"]["LimitParam"];
                /** @description Opaque pagination cursor returned from a previous list call. */
                cursor?: components["parameters"]["CursorParam"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Paginated list of capsule summaries. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapsuleListResponse"];
                };
            };
            500: components["responses"]["InternalError"];
        };
    };
    uploadCapsule: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": {
                    /**
                     * Format: binary
                     * @description The capsule ZIP archive.
                     */
                    capsule: string;
                };
            };
        };
        responses: {
            /** @description Capsule uploaded and unpacked. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapsuleSummary"];
                };
            };
            400: components["responses"]["BadRequest"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["UnprocessableEntity"];
            500: components["responses"]["InternalError"];
        };
    };
    getCapsule: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Capsule detail. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapsuleDetail"];
                };
            };
            404: components["responses"]["NotFound"];
            500: components["responses"]["InternalError"];
        };
    };
    submitCapsuleScore: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    name: string;
                    value: unknown;
                    /** @enum {string} */
                    value_type: "boolean" | "categorical" | "numeric";
                    /** @enum {string} */
                    source: "human" | "heuristic" | "code" | "judge";
                    evaluator_id: string;
                    subject: string;
                    /** @enum {string} */
                    subject_kind?: "span" | "capsule";
                    eval_card_digest: string;
                    score_id?: string | null;
                    supersedes?: string | null;
                    run_id?: string | null;
                    significance?: Record<string, never> | null;
                    /** Format: date-time */
                    created_at?: string;
                };
            };
        };
        responses: {
            /** @description Idempotent replay — score_id already present with an identical body; no second line appended. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Score appended (see schemas/score-submission-response.schema.json). */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        score: Record<string, never>;
                        idempotent_replay: boolean;
                        config_bound: boolean;
                        submission?: {
                            principal?: string;
                            scope?: string;
                            /** Format: date-time */
                            received_at?: string;
                        };
                    };
                };
            };
            400: components["responses"]["BadRequest"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["UnprocessableEntity"];
            500: components["responses"]["InternalError"];
        };
    };
    listLineageNodes: {
        parameters: {
            query?: {
                /** @description Maximum number of items to return (default 50, max 500). */
                limit?: components["parameters"]["LimitParam"];
                /** @description Opaque pagination cursor returned from a previous list call. */
                cursor?: components["parameters"]["CursorParam"];
                kind?: "run" | "asset" | "artifact" | "model" | "tool";
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Paginated list of lineage nodes. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LineageNodeListResponse"];
                };
            };
            500: components["responses"]["InternalError"];
        };
    };
    lineageBlastRadius: {
        parameters: {
            query: {
                /** @description Node reference string (run_id, asset ref, artifact ref). */
                ref: string;
                /** @description Node kind filter (e.g. run, asset). */
                kind?: string;
                depth?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Blast-radius nodes. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LineageQueryResponse"];
                };
            };
            400: components["responses"]["BadRequest"];
            500: components["responses"]["InternalError"];
        };
    };
    lineageProvenance: {
        parameters: {
            query: {
                ref: string;
                kind?: string;
                depth?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Provenance nodes. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LineageQueryResponse"];
                };
            };
            400: components["responses"]["BadRequest"];
            500: components["responses"]["InternalError"];
        };
    };
    lineageReplayChain: {
        parameters: {
            query: {
                run_id: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Replay chain nodes. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReplayChainResponse"];
                };
            };
            400: components["responses"]["BadRequest"];
            500: components["responses"]["InternalError"];
        };
    };
    scheduleReplay: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReplayScheduleRequest"];
            };
        };
        responses: {
            /** @description Replay job accepted; check status via GET /replays/{replay_id}. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReplaySummary"];
                };
            };
            400: components["responses"]["BadRequest"];
            404: components["responses"]["NotFound"];
            500: components["responses"]["InternalError"];
        };
    };
    getReplay: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                replay_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Replay status. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReplaySummary"];
                };
            };
            404: components["responses"]["NotFound"];
            500: components["responses"]["InternalError"];
        };
    };
    cancelReplay: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                replay_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Replay cancelled. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReplaySummary"];
                };
            };
            404: components["responses"]["NotFound"];
            /** @description Replay already completed or cancelled. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            500: components["responses"]["InternalError"];
        };
    };
    replayEvents: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                replay_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /**
             * @description SSE stream. Each event is a JSON object:
             *     `{"event": "progress|completed|error", "data": {...}}`.
             */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            404: components["responses"]["NotFound"];
        };
    };
    exportEvidence: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EvidenceExportRequest"];
            };
        };
        responses: {
            /** @description Evidence export accepted; poll GET /evidence/{bundle_id}. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BundleSummary"];
                };
            };
            400: components["responses"]["BadRequest"];
            404: components["responses"]["NotFound"];
            409: components["responses"]["Conflict"];
            422: components["responses"]["UnprocessableEntity"];
            500: components["responses"]["InternalError"];
        };
    };
    getEvidenceBundle: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                bundle_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Bundle metadata. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BundleSummary"];
                };
            };
            404: components["responses"]["NotFound"];
            500: components["responses"]["InternalError"];
        };
    };
    downloadEvidenceBundle: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                bundle_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description ZIP file binary download. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                };
            };
            404: components["responses"]["NotFound"];
            500: components["responses"]["InternalError"];
        };
    };
    issueDeviceCode: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    /** @example http://localhost:7433 */
                    server_url?: string;
                };
            };
        };
        responses: {
            /** @description Device code issued. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        device_code: string;
                        /** @example ABCD-1234 */
                        user_code: string;
                        verification_uri: string;
                        /** @description Seconds until the device code expires. */
                        expires_in: number;
                        /** @description Minimum polling interval in seconds. */
                        interval: number;
                    };
                };
            };
            500: components["responses"]["InternalError"];
        };
    };
    pollDeviceToken: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    device_code: string;
                };
            };
        };
        responses: {
            /** @description Access token issued. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        access_token: string;
                        /** @example bearer */
                        token_type: string;
                    };
                };
            };
            /** @description Pending approval or expired device code. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            500: components["responses"]["InternalError"];
        };
    };
    approveDeviceCode: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    user_code: string;
                    /** @example user@example.com */
                    subject: string;
                    /**
                     * @example [
                     *       "reader"
                     *     ]
                     */
                    roles?: string[];
                };
            };
        };
        responses: {
            /** @description Device code approved. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        ok?: boolean;
                    };
                };
            };
            400: components["responses"]["BadRequest"];
            500: components["responses"]["InternalError"];
        };
    };
    flushJwksCache: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Cache flushed. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        ok?: boolean;
                        message?: string;
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            500: components["responses"]["InternalError"];
        };
    };
    shardLocalQuery: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    /** @enum {string} */
                    query_type: "provenance" | "blast_radius" | "replay_chain";
                    /** @description ULID run identifier */
                    root_run_id: string;
                    /** @default 5 */
                    max_depth?: number;
                    /** @description Requesting coordinator site_id (audit only) */
                    site_id?: string;
                };
            };
        };
        responses: {
            /** @description Shard-local query result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        site_id: string;
                        rows: {
                            node_id: string;
                            kind: string;
                            ref: string;
                        }[];
                        partial: boolean;
                    };
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    federationQuery: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    /** @enum {string} */
                    query_type: "provenance" | "blast_radius" | "replay_chain";
                    root_run_id: string;
                    /** @default 5 */
                    max_depth?: number;
                    /** @description Shard-local-query base URLs */
                    sites: string[];
                };
            };
        };
        responses: {
            /** @description Federated query result */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        rows: {
                            node_id: string;
                            kind: string;
                            ref: string;
                        }[];
                        partial: boolean;
                        site_count: number;
                        dedup_count: number;
                    };
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    getFederationSummary: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Federation summary */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        entries: {
                            source_site_id: string;
                            target_site_id: string;
                            edge_type: string;
                            count: number;
                        }[];
                        /** @enum {string} */
                        source: "summary";
                        /** Format: date-time */
                        refreshed_at: string;
                    };
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    listApiKeys: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description API keys with derived status. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        api_keys: components["schemas"]["ApiKeyMetadata"][];
                        total: number;
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
        };
    };
    createApiKey: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    /** @description Owning principal (user or svc:<name>). */
                    owner: string;
                    /**
                     * @default [
                     *       "reader"
                     *     ]
                     */
                    roles?: ("reader" | "writer" | "admin" | "auditor")[];
                    /** @description Optional workspace scope (ADR-0178). */
                    workspace?: string | null;
                    expires_in_days?: number | null;
                };
            };
        };
        responses: {
            /** @description Key created; the full key is shown once. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** @description The full nvfk_ key — shown once, store it now. */
                        key: string;
                        api_key: components["schemas"]["ApiKeyMetadata"];
                        note: string;
                    };
                };
            };
            400: components["responses"]["BadRequest"];
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
        };
    };
    revokeApiKey: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                key_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Key revoked. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        ok: boolean;
                        key_id: string;
                        revoked: boolean;
                    };
                };
            };
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            404: components["responses"]["NotFound"];
        };
    };
    rotateApiKey: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                key_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successor minted; the successor key is shown once. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** @description The successor nvfk_ key — shown once. */
                        key: string;
                        api_key: components["schemas"]["ApiKeyMetadata"];
                        overlap_seconds: number;
                        /** Format: date-time */
                        rotate_expires_at: string;
                        note: string;
                    };
                };
            };
            400: components["responses"]["BadRequest"];
            401: components["responses"]["Unauthorized"];
            403: components["responses"]["Forbidden"];
            404: components["responses"]["NotFound"];
        };
    };
}
