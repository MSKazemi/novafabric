/**
 * This file was auto-generated from api/openapi.yaml by
 * scripts/generate-types.mjs (openapi-typescript). Do not edit by hand —
 * run `npm run generate:types` instead. Drift is gated by
 * `npm run check:drift`.
 */
export interface paths {
    "/.well-known/mcp.json": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Mcp Server Card */
        get: operations["mcp_server_card__well_known_mcp_json_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Health */
        get: operations["health_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/scim/v2/Groups": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Groups */
        get: operations["list_groups_scim_v2_Groups_get"];
        put?: never;
        /** Create Group */
        post: operations["create_group_scim_v2_Groups_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/scim/v2/Groups/{group_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Group */
        get: operations["get_group_scim_v2_Groups__group_id__get"];
        /**
         * Put Group
         * @description RFC 7644 §3.5.1 full replace of a Group.
         *
         *     ``displayName`` and ``members`` are replaced atomically (one transaction);
         *     role reconciliation then runs for every member added, removed, or kept —
         *     exactly as PATCH does: new members of a mapped group gain the role, removed
         *     members lose only SCIM-owned grants (ADR-0190), and a revoke that would
         *     remove the last admin surfaces as a SCIM 409 (ADR-0060).
         */
        put: operations["put_group_scim_v2_Groups__group_id__put"];
        post?: never;
        /** Delete Group */
        delete: operations["delete_group_scim_v2_Groups__group_id__delete"];
        options?: never;
        head?: never;
        /** Patch Group */
        patch: operations["patch_group_scim_v2_Groups__group_id__patch"];
        trace?: never;
    };
    "/scim/v2/ResourceTypes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Resource Types */
        get: operations["resource_types_scim_v2_ResourceTypes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/scim/v2/Schemas": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Schemas */
        get: operations["schemas_scim_v2_Schemas_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/scim/v2/ServiceProviderConfig": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Service Provider Config */
        get: operations["service_provider_config_scim_v2_ServiceProviderConfig_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/scim/v2/Users": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Users */
        get: operations["list_users_scim_v2_Users_get"];
        put?: never;
        /** Create User */
        post: operations["create_user_scim_v2_Users_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/scim/v2/Users/{user_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get User */
        get: operations["get_user_scim_v2_Users__user_id__get"];
        put?: never;
        post?: never;
        /** Delete User */
        delete: operations["delete_user_scim_v2_Users__user_id__delete"];
        options?: never;
        head?: never;
        /** Patch User */
        patch: operations["patch_user_scim_v2_Users__user_id__patch"];
        trace?: never;
    };
    "/v0/admin/flush-jwks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Flush Jwks
         * @description Flush the JWKS cache so the next request re-fetches from the issuer.
         *
         *     Requires admin role.
         */
        post: operations["flush_jwks_v0_admin_flush_jwks_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/admin/roles": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Roles
         * @description List all role assignments (admin only).
         */
        get: operations["list_roles_v0_admin_roles_get"];
        put?: never;
        /**
         * Assign Role
         * @description Assign a role to a subject (idempotent). Admin only.
         */
        post: operations["assign_role_v0_admin_roles_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/admin/roles/{subject}/{role}": {
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
         * Revoke Role
         * @description Revoke a role from a subject.
         *
         *     Returns 404 if the assignment did not exist.
         *     Returns 409 if revoking would leave the system with no admin path (lockout guard).
         */
        delete: operations["revoke_role_v0_admin_roles__subject___role__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/api-keys": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Api Keys
         * @description List API keys (admin only) — metadata only, never secrets or hashes.
         */
        get: operations["list_api_keys_v0_api_keys_get"];
        put?: never;
        /**
         * Create Api Key
         * @description Create an API key (admin only). The key is returned ONCE and never stored.
         */
        post: operations["create_api_key_v0_api_keys_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/api-keys/{key_id}": {
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
         * Revoke Api Key
         * @description Revoke an API key by key_id (admin only) — effective on the next request.
         */
        delete: operations["revoke_api_key_v0_api_keys__key_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/api-keys/{key_id}/rotate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Rotate Api Key
         * @description Rotate an API key (admin only). The successor is returned ONCE.
         *
         *     Both keys stay valid for the bounded overlap window; the predecessor
         *     auto-revokes at verify time once the window elapses (ADR-0193 D3).
         */
        post: operations["rotate_api_key_v0_api_keys__key_id__rotate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/assets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List assets */
        get: operations["listAssets"];
        put?: never;
        /** Register an asset from a YAML spec */
        post: operations["createAsset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/assets/{asset_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get an asset by UUID */
        get: operations["getAsset"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/assets/{asset_id}/promote": {
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
    "/v0/auth/saml/acs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Assertion Consumer Service
         * @description Assertion Consumer Service (HTTP-POST binding).
         *
         *     Refuses (501) unless the experimental ACS is opted in. When enabled: verify
         *     the XML-DSIG signature and parse under an XXE-hardened parser (V1/V2/V10),
         *     apply the policy rules (V3–V9, V11), map attributes to roles, resolve the
         *     subject, and mint a bearer token. Any failure rejects with 401 and issues no
         *     session; error messages never carry assertion contents (the closed audit rule).
         */
        post: operations["assertion_consumer_service_v0_auth_saml_acs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/auth/saml/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Sp Initiated Login
         * @description SP-initiated SSO entry point.
         *
         *     Refuses (501) unless the experimental ACS is opted in (a flow whose assertion
         *     could never be verified would strand the user at the IdP). When enabled,
         *     redirects (302) to the IdP SSO URL with a deflated, base64-encoded
         *     ``SAMLRequest`` (HTTP-Redirect binding).
         */
        get: operations["sp_initiated_login_v0_auth_saml_login_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/auth/saml/metadata": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Sp Metadata
         * @description Emit this SP's SAML metadata XML (entity ID, ACS URL, SP cert).
         *
         *     Read-only, unauthenticated (metadata is public by design), no side effects.
         */
        get: operations["sp_metadata_v0_auth_saml_metadata_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/capsules": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List run capsules
         * @description List capsules — keyset (seek) pagination, ADR-0206 P1 (experimental).
         *
         *     Sort order is pinned to ``created_at DESC, run_id DESC``; ``next_cursor``
         *     is an opaque v1 keyset cursor. Legacy ``{"offset": N}`` cursors are still
         *     served by the old materialized path for one deprecation cycle (ADR-0188)
         *     and carry a ``Deprecation: true`` response header. A non-empty cursor
         *     that fails strict decoding is a 400 ``invalid_cursor``.
         */
        get: operations["listCapsules"];
        put?: never;
        /**
         * Upload a run capsule archive
         * @description Accept a capsule ZIP, spool it to disk, unpack it under the capsule_dir.
         *
         *     ADR-0203 P1 (experimental): the body is size-capped (413
         *     ``payload_too_large``), streamed to a disk spool in bounded chunks,
         *     guarded against zip bombs and ``..``-traversal members (422
         *     ``zip_guard_violation``), extracted member-by-member into a temp
         *     directory, and published atomically via rename — a failed request never
         *     leaves a partial ``capsule_dir/<run_id>``.
         */
        post: operations["uploadCapsule"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/capsules/bulk-delete": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Bulk Delete Capsules
         * @description Bounded bulk delete with per-item outcomes (ADR-0206 D3, experimental).
         *
         *     Batch size is capped by ``server.bulk.max_items`` (default 100, ceiling
         *     1000) — an oversized batch is a 422 before any work. Each id runs the
         *     full single-delete pipeline; outcomes are reported per item
         *     (``deleted | held | not_found | invalid_id | duplicate | error``), with
         *     no transactional rollback — partial progress is visible, not undone.
         *     ``dry_run`` returns the identical report while deleting nothing.
         */
        post: operations["bulk_delete_capsules_v0_capsules_bulk_delete_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/capsules/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get a run capsule by run_id */
        get: operations["getCapsule"];
        put?: never;
        post?: never;
        /**
         * Delete Capsule
         * @description Delete one capsule — admin-only, hold-refusing, audited (ADR-0206 D2).
         *
         *     Legal holds always win: any unreleased hold in any registry refuses with
         *     409 ``legal_hold_active`` (holds are registry-global today — documented
         *     limit), and there is no force override. An unexpired WORM lock refuses
         *     with 409 ``worm_hold``. Deleting an already-deleted id is a 404 (delete
         *     is evidenced, not idempotent-silent).
         */
        delete: operations["delete_capsule_v0_capsules__run_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/capsules/{run_id}/scores": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Append an externally-computed score to a capsule
         * @description Append one externally-computed score to the capsule's ``scores.jsonl``.
         *
         *     ADR-0119 D2: the same validation core as the local SDK/CLI, exposed over the
         *     v0.7 REST API. Append-only and fail-closed (a rejection writes nothing);
         *     idempotent by ``score_id`` (identical replay → 200, no second line). The
         *     ``scores:write`` capability maps to the ``writer`` role in the shipped RBAC
         *     model; the authenticated principal is recorded in the ``submission`` block so
         *     *who submitted* stays distinguishable from *what evaluator produced the value*.
         */
        post: operations["submitCapsuleScore"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/evidence": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Export a capsule as an Evidence Bundle */
        post: operations["exportEvidence"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/evidence/{bundle_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get evidence-bundle metadata */
        get: operations["getEvidenceBundle"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/evidence/{bundle_id}/download": {
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
    "/v0/lineage/blast-radius": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Blast Radius */
        get: operations["blast_radius_v0_lineage_blast_radius_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/lineage/nodes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Lineage Nodes */
        get: operations["list_lineage_nodes_v0_lineage_nodes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/lineage/provenance": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Provenance */
        get: operations["provenance_v0_lineage_provenance_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/lineage/replay-chain": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Replay Chain */
        get: operations["replay_chain_v0_lineage_replay_chain_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/orgs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Orgs
         * @description List all organizations (admin only).
         */
        get: operations["list_orgs_v0_orgs_get"];
        put?: never;
        /**
         * Create Org
         * @description Create an organization (admin only).
         */
        post: operations["create_org_v0_orgs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/orgs/{org_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Org
         * @description Fetch one organization (admin only).
         */
        get: operations["get_org_v0_orgs__org_id__get"];
        put?: never;
        post?: never;
        /**
         * Delete Org
         * @description Delete an empty organization (admin only).
         *
         *     404 if unknown; 409 if it still contains workspaces or memberships.
         */
        delete: operations["delete_org_v0_orgs__org_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/replays": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Schedule Replay */
        post: operations["schedule_replay_v0_replays_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/replays/{replay_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Replay */
        get: operations["get_replay_v0_replays__replay_id__get"];
        put?: never;
        post?: never;
        /** Cancel Replay */
        delete: operations["cancel_replay_v0_replays__replay_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/replays/{replay_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Replay Events */
        get: operations["replay_events_v0_replays__replay_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/runs/suggest-register": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Registration Suggestions
         * @description Aggregate asset registration suggestions across the most recent capsules.
         *
         *     Skips assets already present in the registry.
         */
        get: operations["get_registration_suggestions_v0_runs_suggest_register_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/seal/policy": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Seal Policy
         * @description Return the latest promotion policy predicate, or 404 if none exists.
         */
        get: operations["get_seal_policy_v0_seal_policy_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/seal/{capsule_id}/proposals": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Seal Proposals
         * @description List all proposals for a capsule with their approval status.
         */
        get: operations["list_seal_proposals_v0_seal__capsule_id__proposals_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/seal/{capsule_id}/verify": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Verify Seal Chain
         * @description Run the five-check SoD verifier for a capsule's promote bundles.
         */
        post: operations["verify_seal_chain_v0_seal__capsule_id__verify_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/service-accounts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Service Accounts
         * @description List service accounts (admin only). Tokens are never included.
         */
        get: operations["list_service_accounts_v0_service_accounts_get"];
        put?: never;
        /**
         * Create Service Account
         * @description Create a service account and mint its offline token (admin only).
         *
         *     The token is returned ONCE in this response and is not retrievable later.
         *     It carries no roles of its own — effective roles come from the account's
         *     workspace/org memberships or global role assignments (ADR-0178 I3).
         */
        post: operations["create_service_account_v0_service_accounts_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/service-accounts/{account_id}/disable": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Disable Service Account
         * @description Disable a service account and revoke its outstanding token (admin only).
         *
         *     Revocation reuses the existing ``token_audit`` path; independently, the
         *     disabled flag is checked at token verification time (spec I4).
         */
        post: operations["disable_service_account_v0_service_accounts__account_id__disable_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/usage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Usage
         * @description Per-workspace usage for one period (default: current UTC period).
         */
        get: operations["get_usage_v0_usage_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/version": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Version */
        get: operations["version_v0_version_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/webhooks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Webhooks
         * @description List subscriptions (admin, auditor) — metadata only, never secrets.
         */
        get: operations["list_webhooks_v0_webhooks_get"];
        put?: never;
        /**
         * Create Webhook
         * @description Create a subscription (admin only). The signing secret is returned ONCE.
         */
        post: operations["create_webhook_v0_webhooks_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/webhooks/{hook_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Webhook
         * @description Get one subscription (admin, auditor); unknown → 404.
         */
        get: operations["get_webhook_v0_webhooks__hook_id__get"];
        put?: never;
        post?: never;
        /**
         * Delete Webhook
         * @description Delete a subscription (admin only); delivery rows remain until pruned.
         */
        delete: operations["delete_webhook_v0_webhooks__hook_id__delete"];
        options?: never;
        head?: never;
        /**
         * Update Webhook
         * @description Update url/description/event_types/workspace/disabled (admin only).
         *
         *     The signing secret is NOT updatable (400) — rotate is P2 (ADR-0205 D2).
         */
        patch: operations["update_webhook_v0_webhooks__hook_id__patch"];
        trace?: never;
    };
    "/v0/webhooks/{hook_id}/deliveries": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Deliveries
         * @description Cursor-paginated delivery-attempt log, newest first (admin, auditor).
         */
        get: operations["list_deliveries_v0_webhooks__hook_id__deliveries_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/webhooks/{hook_id}/deliveries/{delivery_id}/redeliver": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Redeliver
         * @description Re-enqueue a terminal-failed delivery (admin). Non-terminal → 409.
         */
        post: operations["redeliver_v0_webhooks__hook_id__deliveries__delivery_id__redeliver_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/webhooks/{hook_id}/ping": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Ping Webhook
         * @description Send a synthetic ``webhook.ping`` through the full delivery path (admin).
         */
        post: operations["ping_webhook_v0_webhooks__hook_id__ping_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/workspaces": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Workspaces
         * @description List workspaces, optionally filtered by org (admin only).
         */
        get: operations["list_workspaces_v0_workspaces_get"];
        put?: never;
        /**
         * Create Workspace
         * @description Create a workspace in an organization (admin only).
         */
        post: operations["create_workspace_v0_workspaces_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/workspaces/{ws_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Workspace
         * @description Fetch one workspace with its memberships (admin only).
         */
        get: operations["get_workspace_v0_workspaces__ws_id__get"];
        put?: never;
        post?: never;
        /**
         * Delete Workspace
         * @description Delete an empty workspace (admin only). 409 if it has memberships.
         */
        delete: operations["delete_workspace_v0_workspaces__ws_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/workspaces/{ws_id}/memberships": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Grant Membership
         * @description Grant a workspace-scoped role binding (admin only). Idempotent.
         */
        post: operations["grant_membership_v0_workspaces__ws_id__memberships_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v0/workspaces/{ws_id}/memberships/{principal}/{role}": {
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
         * Revoke Membership
         * @description Revoke a workspace-scoped role binding (admin only). 404 if absent.
         */
        delete: operations["revoke_membership_v0_workspaces__ws_id__memberships__principal___role__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AssetDetail
         * @description A single asset with its spec and promotion provenance.
         */
        AssetDetail: {
            asset_type: components["schemas"]["AssetType"];
            /** Created At */
            created_at: string;
            /**
             * Forced Promotion
             * @default false
             */
            forced_promotion: boolean;
            /** Git Commit Sha */
            git_commit_sha?: string | null;
            /** Id */
            id: string;
            /** Name */
            name: string;
            /** Promoted At */
            promoted_at?: string | null;
            /** Promoted By */
            promoted_by?: string | null;
            /**
             * Spec Json
             * @description Raw JSON-serialized asset spec.
             */
            spec_json?: string | null;
            status: components["schemas"]["AssetStatus"];
            /**
             * Version
             * @description SemVer string.
             */
            version: string;
        };
        /**
         * AssetListResponse
         * @description ``GET /v0/assets``.
         */
        AssetListResponse: {
            /** Items */
            items: components["schemas"]["AssetSummary"][];
            /**
             * Next Cursor
             * @description Opaque cursor for the next page; null when no more pages.
             */
            next_cursor?: string | null;
            /**
             * Total
             * @description Total count of matching items, without pagination applied. Present on the first page only; keyset pages omit it by design.
             */
            total?: number | null;
        };
        /**
         * AssetStatus
         * @enum {string}
         */
        AssetStatus: "development" | "validated" | "pending_approval" | "staging" | "production" | "archived";
        /**
         * AssetSummary
         * @description One asset as returned by the list endpoint.
         */
        AssetSummary: {
            asset_type: components["schemas"]["AssetType"];
            /** Created At */
            created_at: string;
            /** Git Commit Sha */
            git_commit_sha?: string | null;
            /** Id */
            id: string;
            /** Name */
            name: string;
            /** Promoted At */
            promoted_at?: string | null;
            status: components["schemas"]["AssetStatus"];
            /**
             * Version
             * @description SemVer string.
             */
            version: string;
        };
        /**
         * AssetType
         * @enum {string}
         */
        AssetType: "model" | "agent" | "prompt" | "tool" | "dataset" | "evaluation" | "deployment";
        /** AssignRoleRequest */
        AssignRoleRequest: {
            role: components["schemas"]["Role"];
            /** Subject */
            subject: string;
        };
        /** Body_uploadCapsule */
        Body_uploadCapsule: {
            /** Capsule */
            capsule: string;
        };
        /**
         * BulkDeleteRequest
         * @description Body of ``POST /v0/capsules/bulk-delete`` (spec bulk-ops-pagination-v0).
         */
        BulkDeleteRequest: {
            /**
             * Dry Run
             * @default false
             */
            dry_run: boolean;
            /** Run Ids */
            run_ids?: string[];
        };
        /**
         * BundleSummary
         * @description Evidence-bundle metadata.
         */
        BundleSummary: {
            /** Bundle Id */
            bundle_id: string;
            /** Bundle Path */
            bundle_path?: string | null;
            /** Created At */
            created_at: string;
            /** Run Id */
            run_id: string;
            /** Size Bytes */
            size_bytes: number;
        };
        /**
         * CapsuleDetail
         * @description A single run capsule, with the manifest fields the list view omits.
         */
        CapsuleDetail: {
            /** Capture Mode */
            capture_mode?: string | null;
            /** Command */
            command?: string[] | null;
            /** Created At */
            created_at?: string | null;
            /** Duration Ms */
            duration_ms?: number | null;
            /** Exit Code */
            exit_code?: number | null;
            /** Finished At */
            finished_at?: string | null;
            /** Model Call Count */
            model_call_count?: number | null;
            /** Mutating Tool Count */
            mutating_tool_count?: number | null;
            /** Novafabric Version */
            novafabric_version?: string | null;
            /** Run Id */
            run_id: string;
            /** Schema Version */
            schema_version?: string | null;
            /**
             * Status
             * @description Run outcome; 'unknown' when the manifest does not record one.
             */
            status: string;
            /** Tool Call Count */
            tool_call_count?: number | null;
        };
        /**
         * CapsuleListResponse
         * @description ``GET /v0/capsules``.
         */
        CapsuleListResponse: {
            /** Items */
            items: components["schemas"]["CapsuleSummary"][];
            /**
             * Next Cursor
             * @description Opaque cursor for the next page; null when no more pages.
             */
            next_cursor?: string | null;
            /**
             * Total
             * @description Total count of matching items, without pagination applied. Present on the first page only; keyset pages omit it by design.
             */
            total?: number | null;
        };
        /**
         * CapsuleSummary
         * @description One run capsule as returned by the list endpoint.
         */
        CapsuleSummary: {
            /** Command */
            command?: string[] | null;
            /** Created At */
            created_at?: string | null;
            /** Duration Ms */
            duration_ms?: number | null;
            /** Exit Code */
            exit_code?: number | null;
            /** Finished At */
            finished_at?: string | null;
            /** Run Id */
            run_id: string;
            /**
             * Status
             * @description Run outcome; 'unknown' when the manifest does not record one.
             */
            status: string;
        };
        /** CreateApiKeyRequest */
        CreateApiKeyRequest: {
            /** Expires In Days */
            expires_in_days?: number | null;
            /** Owner */
            owner: string;
            /** Roles */
            roles?: string[];
            /** Workspace */
            workspace?: string | null;
        };
        /** CreateOrgRequest */
        CreateOrgRequest: {
            /** Name */
            name: string;
            /** Slug */
            slug: string;
        };
        /** CreateServiceAccountRequest */
        CreateServiceAccountRequest: {
            /**
             * Description
             * @default
             */
            description: string;
            /**
             * Expires In Days
             * @default 90
             */
            expires_in_days: number;
            /** Name */
            name: string;
        };
        /** CreateWebhookRequest */
        CreateWebhookRequest: {
            /**
             * Description
             * @default
             */
            description: string;
            /**
             * Disabled
             * @default false
             */
            disabled: boolean;
            /** Event Types */
            event_types?: string[] | null;
            /** Url */
            url: string;
            /** Workspace */
            workspace?: string | null;
        };
        /** CreateWorkspaceRequest */
        CreateWorkspaceRequest: {
            /** Name */
            name: string;
            /** Org Id */
            org_id: string;
            /** Slug */
            slug: string;
        };
        /**
         * ErrorDetail
         * @description The inner object of the standard error envelope.
         */
        ErrorDetail: {
            /**
             * Code
             * @example not_found
             */
            code: string;
            /**
             * Details
             * @description Structured, machine-readable context for the error.
             */
            details?: {
                [key: string]: unknown;
            };
            /**
             * Message
             * @example Asset 'my-agent@v1.0' not found.
             */
            message: string;
        };
        /**
         * ErrorEnvelope
         * @description Every non-2xx response body: ``{"error": {"code", "message", "details"}}``.
         */
        ErrorEnvelope: {
            error: components["schemas"]["ErrorDetail"];
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /** MembershipRequest */
        MembershipRequest: {
            /** Principal */
            principal: string;
            /** Role */
            role: string;
        };
        /** PolicyResponse */
        PolicyResponse: {
            /** Created At */
            created_at: string;
            /** Predicate */
            predicate: {
                [key: string]: unknown;
            };
            /** Version */
            version: number;
        };
        /** ProposalSummary */
        ProposalSummary: {
            /** Approval Timestamp */
            approval_timestamp?: string | null;
            /** Approver Subject */
            approver_subject?: string | null;
            /** Capsule Id */
            capsule_id: string;
            /** Has Approval */
            has_approval: boolean;
            /** Justification */
            justification: string;
            /** Policy Version */
            policy_version: string;
            /** Proposer Subject */
            proposer_subject: string;
            /** Timestamp */
            timestamp: string;
            /** Uuid */
            uuid: string;
        };
        /**
         * Role
         * @enum {string}
         */
        Role: "reader" | "writer" | "admin" | "auditor";
        /**
         * ScoreSubmissionResult
         * @description ``POST /v0/capsules/{run_id}/scores``.
         *
         *     Returned for **both** 201 (appended) and 200 (idempotent replay). The
         *     previous published contract declared the 200 as bodiless; the route returns
         *     the same body either way and only the status code differs.
         */
        ScoreSubmissionResult: {
            /** Config Bound */
            config_bound: boolean;
            /** Idempotent Replay */
            idempotent_replay: boolean;
            /**
             * Score
             * @description The stored score record.
             */
            score: {
                [key: string]: unknown;
            };
            /**
             * Submission
             * @description Who submitted, as distinct from what evaluator produced the value: principal, scope and received_at.
             */
            submission?: {
                [key: string]: unknown;
            };
        };
        /** UpdateWebhookRequest */
        UpdateWebhookRequest: {
            /** Description */
            description?: string | null;
            /** Disabled */
            disabled?: boolean | null;
            /** Event Types */
            event_types?: string[] | null;
            /** Secret */
            secret?: string | null;
            /** Url */
            url?: string | null;
            /** Workspace */
            workspace?: string | null;
        };
        /** ValidationError */
        ValidationError: {
            /** Context */
            ctx?: Record<string, never>;
            /** Input */
            input?: unknown;
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
        };
        /** VerifyResponse */
        VerifyResponse: {
            /** Capsule Id */
            capsule_id: string;
            /** Check Results */
            check_results: {
                [key: string]: unknown;
            }[];
            /** Exit Code */
            exit_code: number;
            /** Message */
            message: string;
            /** Passed */
            passed: boolean;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    mcp_server_card__well_known_mcp_json_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    health_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    list_groups_scim_v2_Groups_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    create_group_scim_v2_Groups_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_group_scim_v2_Groups__group_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                group_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_group_scim_v2_Groups__group_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                group_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_group_scim_v2_Groups__group_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                group_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    patch_group_scim_v2_Groups__group_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                group_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    resource_types_scim_v2_ResourceTypes_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    schemas_scim_v2_Schemas_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    service_provider_config_scim_v2_ServiceProviderConfig_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    list_users_scim_v2_Users_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    create_user_scim_v2_Users_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_user_scim_v2_Users__user_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                user_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_user_scim_v2_Users__user_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                user_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    patch_user_scim_v2_Users__user_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                user_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    flush_jwks_v0_admin_flush_jwks_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    list_roles_v0_admin_roles_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    assign_role_v0_admin_roles_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AssignRoleRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    revoke_role_v0_admin_roles__subject___role__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                subject: string;
                role: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_api_keys_v0_api_keys_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    create_api_key_v0_api_keys_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateApiKeyRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    revoke_api_key_v0_api_keys__key_id__delete: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    rotate_api_key_v0_api_keys__key_id__rotate_post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    listAssets: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
                asset_type?: string | null;
                status?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description A page of assets. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetListResponse"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
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
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description The registered asset. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetDetail"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The request conflicts with the current state of the resource. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    getAsset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The asset. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetDetail"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    promoteAsset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description The promoted asset. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssetDetail"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The request conflicts with the current state of the resource. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description A precondition on the request was not met. */
            412: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    assertion_consumer_service_v0_auth_saml_acs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    sp_initiated_login_v0_auth_saml_login_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    sp_metadata_v0_auth_saml_metadata_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    listCapsules: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description A page of run capsules. `total` is present on the first page only — keyset pages omit it by design (ADR-0206). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapsuleListResponse"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
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
                "multipart/form-data": components["schemas"]["Body_uploadCapsule"];
            };
        };
        responses: {
            /** @description The stored capsule. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapsuleSummary"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The request conflicts with the current state of the resource. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The request body exceeds the configured size limit. */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    bulk_delete_capsules_v0_capsules_bulk_delete_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BulkDeleteRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
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
            /** @description The run capsule. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapsuleDetail"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_capsule_v0_capsules__run_id__delete: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
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
        /** @description The externally-computed evaluation record to append (ADR-0119). */
        requestBody: {
            content: {
                "application/json": {
                    /** Created At */
                    created_at?: string | null;
                    /** Eval Card Digest */
                    eval_card_digest: string;
                    /** Evaluator Id */
                    evaluator_id: string;
                    /** Name */
                    name: string;
                    /** Run Id */
                    run_id?: string | null;
                    /** Score Id */
                    score_id?: string | null;
                    significance?: {
                        /** Ci High */
                        ci_high: number;
                        /** Ci Low */
                        ci_low: number;
                        /** Confidence */
                        confidence: number;
                        /** Method */
                        method: string;
                    } | null;
                    /**
                     * ScoreSource
                     * @description Provenance of a score (requirement 1, NF-010).
                     *
                     *     ``heuristic`` and ``code`` scorers run offline on stored capsules at zero token
                     *     cost; only ``judge`` scorers may spend tokens, and only on sampled subjects.
                     * @enum {string}
                     */
                    source: "human" | "heuristic" | "code" | "judge";
                    /** Subject */
                    subject: string;
                    /**
                     * Subject Kind
                     * @default span
                     */
                    subject_kind?: string;
                    /** Supersedes */
                    supersedes?: string | null;
                    /** Value */
                    value: boolean | number | string;
                    /**
                     * ScoreValueType
                     * @description The type of a score value (requirement 1).
                     * @enum {string}
                     */
                    value_type: "boolean" | "categorical" | "numeric";
                };
            };
        };
        responses: {
            /** @description Idempotent replay — `score_id` was already present with an identical body, so no second line was appended. The body is the same as the 201; only the status differs. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScoreSubmissionResult"];
                };
            };
            /** @description Score appended. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScoreSubmissionResult"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The request conflicts with the current state of the resource. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    exportEvidence: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** @description The capsule to seal into an Evidence Bundle. */
        requestBody: {
            content: {
                "application/json": {
                    /**
                     * Allow Unsafe Skips
                     * @default false
                     */
                    allow_unsafe_skips?: boolean;
                    /**
                     * Output Path
                     * @description Optional override for the output ZIP path (server-side path).
                     */
                    output_path?: string | null;
                    /**
                     * Run Id
                     * @description The capsule run_id to export as evidence.
                     */
                    run_id: string;
                } & {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Bundle accepted; metadata for polling and download. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BundleSummary"];
                };
            };
            /** @description Invalid request parameters or body. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The request conflicts with the current state of the resource. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
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
            /** @description The bundle metadata. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BundleSummary"];
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
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
            /** @description The bundle archive. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                    "application/zip": unknown;
                };
            };
            /** @description Missing or invalid bearer token. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Authenticated but not permitted (missing role). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description The requested resource does not exist. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    blast_radius_v0_lineage_blast_radius_get: {
        parameters: {
            query: {
                ref: string;
                kind?: string | null;
                depth?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_lineage_nodes_v0_lineage_nodes_get: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
                kind?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    provenance_v0_lineage_provenance_get: {
        parameters: {
            query: {
                ref: string;
                kind?: string | null;
                depth?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    replay_chain_v0_lineage_replay_chain_get: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_orgs_v0_orgs_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    create_org_v0_orgs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateOrgRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_org_v0_orgs__org_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                org_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_org_v0_orgs__org_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                org_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    schedule_replay_v0_replays_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_replay_v0_replays__replay_id__get: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    cancel_replay_v0_replays__replay_id__delete: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    replay_events_v0_replays__replay_id__events_get: {
        parameters: {
            query?: {
                "Last-Event-ID"?: string | null;
            };
            header?: never;
            path: {
                replay_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_registration_suggestions_v0_runs_suggest_register_get: {
        parameters: {
            query?: {
                capsule_limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_seal_policy_v0_seal_policy_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PolicyResponse"];
                };
            };
        };
    };
    list_seal_proposals_v0_seal__capsule_id__proposals_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                capsule_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProposalSummary"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    verify_seal_chain_v0_seal__capsule_id__verify_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                capsule_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VerifyResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_service_accounts_v0_service_accounts_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    create_service_account_v0_service_accounts_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateServiceAccountRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    disable_service_account_v0_service_accounts__account_id__disable_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                account_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_usage_v0_usage_get: {
        parameters: {
            query?: {
                period?: string | null;
                workspace?: string | null;
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    version_v0_version_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    list_webhooks_v0_webhooks_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    create_webhook_v0_webhooks_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateWebhookRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_webhook_v0_webhooks__hook_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                hook_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_webhook_v0_webhooks__hook_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                hook_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_webhook_v0_webhooks__hook_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                hook_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UpdateWebhookRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_deliveries_v0_webhooks__hook_id__deliveries_get: {
        parameters: {
            query?: {
                status?: string | null;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                hook_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    redeliver_v0_webhooks__hook_id__deliveries__delivery_id__redeliver_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                hook_id: string;
                delivery_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    ping_webhook_v0_webhooks__hook_id__ping_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                hook_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_workspaces_v0_workspaces_get: {
        parameters: {
            query?: {
                org_id?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_workspace_v0_workspaces_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateWorkspaceRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_workspace_v0_workspaces__ws_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                ws_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_workspace_v0_workspaces__ws_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                ws_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    grant_membership_v0_workspaces__ws_id__memberships_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                ws_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MembershipRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    revoke_membership_v0_workspaces__ws_id__memberships__principal___role__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                ws_id: string;
                principal: string;
                role: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
