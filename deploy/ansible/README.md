# Single-host Ansible role: NovaFabric server under systemd

**Status: experimental.** The role installs and runs `nova server start` (server mode,
itself experimental) on one Linux host. CI runs `ansible-lint` (production profile) and
`ansible-playbook --syntax-check` on every change under `deploy/ansible/`. CI does not
yet install it on a real host or check that a second run changes nothing (no Molecule
job yet). Read the tasks before you run them; they are short on purpose.

This is separate from [`deploy/hpc/ansible/`](../hpc/ansible/install.yml), which
deploys Slurm prolog/epilog scripts and is unchanged.

## What it does

| Step | How |
|---|---|
| Service account | System user and group `novafabric`, shell `/usr/sbin/nologin`, home = the data dir |
| Package | `novafabric[server]==<novafabric_version>` from PyPI (or your mirror) into `/opt/novafabric/venv`, owned by root. There is no default version: you pin it |
| Config | `/etc/novafabric/server.yaml` (0640 root:novafabric, no secrets), `novafabric.env` (0644, paths), `secrets.env` (0600 root:root, DSN and tokens from your vault) |
| Migrations | `nova db upgrade --track registry --backend <backend>` as a handler, run only when the package or the config changed, always before the server (re)starts |
| Service | `novafabric.service`: `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=` the data dir only, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, an empty capability set, `@system-service` syscall filter, `Restart=on-failure` |
| Health | Waits for `GET /readyz` to answer 200 (database reachable, schema at head) |
| Backups (opt-in) | `novafabric-backup.timer` runs `nova backup create --home … -o <backup dir>/` daily |
| Restore | `restore.yml`: `nova backup verify` → stop → `nova restore` → registry migration → start → `/readyz` |
| Uninstall | `uninstall.yml`: removes the units and the virtualenv. Data, backups and the user are always kept |

What the role leaves to you: Postgres (it only takes a DSN), TLS certificates, a
reverse proxy, firewalling, and where backup sets go after they are written.

## Layout on the host

| Path | Contents | Writable by the service |
|---|---|---|
| `/var/lib/novafabric/` (`novafabric_data_dir`) | `HOME` of the service | yes, the only path |
| `/var/lib/novafabric/home/` (`novafabric_home`) | `NOVAFABRIC_HOME`: registry, capsules, keys, seal state, `.server-token` | yes |
| `/var/lib/novafabric/audit/audit.jsonl` | the hash-chained audit log (`NOVAFABRIC_AUDIT_LOG_PATH`; `NOVAFABRIC_HOME` does not move it) | yes |
| `/opt/novafabric/venv/` | the pinned package | no |
| `/etc/novafabric/` | config and secrets | no |
| `/var/backups/novafabric/` | backup sets, when the timer is on | only the backup unit |

## Requirements

- Controller: `ansible-core` 2.16 or newer. The role uses `ansible.builtin` modules only,
  no collections.
- Target: systemd and Python 3.12 or newer (the role checks this). The default OS package
  list (`python3`, `python3-venv`, `python3-packaging`, `acl`) uses Debian/Ubuntu names. The
  role is written for Ubuntu 24.04 and Debian 13; neither has been run in CI yet. On other
  distributions, override `novafabric_os_packages` and `novafabric_python`.
- Postgres backend: a database you run, and `pg_dump`/`pg_restore` on the host if you
  use `--profile pg` backups or restore a pg set.

## Quick start

```bash
cd deploy/ansible
cp inventory.example.yml inventory.yml        # set the host and novafabric_version
mkdir -p group_vars/novafabric_servers
ansible-vault create group_vars/novafabric_servers/vault.yml
ansible-playbook -i inventory.yml site.yml --ask-vault-pass
```

The vault file holds the secrets. Do not commit `inventory.yml` or `group_vars/` back into a
clone of this repository. The role has no default for any of the secrets:

```yaml
# group_vars/novafabric_servers/vault.yml (encrypted)
novafabric_postgres_dsn: "postgresql://nova:…@db.internal:5432/novafabric"  # postgres backend only
novafabric_server_token: "…at least 32 random characters…"                  # optional
novafabric_scim_token: "…"                                                   # optional, SCIM only
```

Without `novafabric_server_token` the server generates a local bearer token on first
start and keeps it at `/var/lib/novafabric/home/.server-token` (mode 0600). Under
systemd only its fingerprint is logged; read the file to get the token.

Run the playbook again whenever you like. Every task is written to converge: with
unchanged inputs a re-run should report no changes and not restart the service (CI does
not check this on a host yet). To upgrade, change `novafabric_version` and re-run:
pip installs the new release, the handler migrates the registry, then the service
restarts. With the SQLite backend the service is stopped before the migration; with
Postgres the migration runs while the old process is still serving, as the
[upgrade guide](../../docs/ops/upgrade-guide.md) describes. Back up first (below).

### Why only the registry track

`nova server start` opens the registry database and refuses to start when its schema
is behind (the schema-skew guard checks the registry track). The role does not run the
MetadataStore track (`nova db upgrade` without `--track`): on Postgres both tracks keep
their stamp in the same `alembic_version` table, and running both against one database
makes the server refuse to start. See section 4a of the
[upgrade guide](../../docs/ops/upgrade-guide.md).

## Variables

All inputs, with defaults, are in [`roles/novafabric/defaults/main.yml`](roles/novafabric/defaults/main.yml).
The ones you are most likely to set:

| Variable | Default | Notes |
|---|---|---|
| `novafabric_version` | none (required) | exact release, for example `"0.109.0"` |
| `novafabric_backend` | `sqlite` | `postgres` needs `novafabric_postgres_dsn` |
| `novafabric_host` / `novafabric_port` | `127.0.0.1` / `7433` | a port below 1024 adds `CAP_NET_BIND_SERVICE` and nothing else |
| `novafabric_workers` | `1` | more than 1 needs Postgres |
| `novafabric_oidc_issuer_url` / `novafabric_oidc_audience` | empty | OIDC is on when both are set |
| `novafabric_tls_cert_path` / `novafabric_tls_key_path` | empty | native TLS; the key must be owned by `novafabric` at 0600 |
| `novafabric_rate_limits_enabled` | `false` | the server guide recommends `true` for any non-loopback bind |
| `novafabric_server_config_extra` | `{}` | more top-level `server.yaml` keys; secret keys are refused |
| `novafabric_pip_find_links` | empty | a local wheel directory on the target; installs with `--no-index` |
| `novafabric_pip_index_url` | empty | an internal package mirror |
| `novafabric_backup_timer_enabled` | `false` | daily `nova backup create`; `novafabric_backup_profile: pg` for Postgres |
| `novafabric_no_log` | `true` | hides task arguments and environment (DSN, tokens) from Ansible output |

### Offline installs

Copy the wheels for `novafabric[server]==<version>` and its dependencies to a directory
on the target (see the [air-gapped install guide](../../docs/ops/air-gapped-install.md))
and set `novafabric_pip_find_links` to it. The role then installs with
`pip --no-index --find-links <dir>` and needs no network access.

## Backups

With `novafabric_backup_timer_enabled: true` the role installs a daily timer that runs
`nova backup create --home /var/lib/novafabric/home --profile local -o /var/backups/novafabric/`
as the service user. SQLite stores are copied with the online-backup API, so the server
keeps running. The role does not prune old sets or copy them off the host: put
`novafabric_backup_dir` on another volume and ship the sets somewhere else yourself. A set
holds the PII key store (`dek.db`), so treat it as sensitive. Signing keys are left out
unless `novafabric_backup_include_keys: true`. See
[Backup and restore](../../docs/ops/backup-restore.md).

## Recovery

```bash
# a set already on the host
ansible-playbook -i inventory.yml restore.yml --limit nova01 \
  -e novafabric_restore_set=/var/backups/novafabric/nova-backup-<set_id>.tar.gz

# a set on the controller, copied to the host first
ansible-playbook -i inventory.yml restore.yml --limit nova01 \
  -e novafabric_restore_set_src=./nova-backup-<set_id>.tar.gz -e novafabric_restore_force=true
```

The playbook runs `nova backup verify` first, so a damaged set stops it before anything
is touched. Then it stops the service and runs
`nova restore <set> --home /var/lib/novafabric/home`, which verifies, extracts, migrates
and runs its own verification chain, and exits 1 if any step fails. Then it brings the
registry to this release's head and starts the service.

- `nova restore` refuses a non-empty home (or, for a pg set, a non-empty database).
  `-e novafabric_restore_force=true` moves the existing data aside into
  `/var/lib/novafabric/home/.pre-restore-<timestamp>/`; nothing is deleted, and a pg
  restore takes a safety dump first. A host that `site.yml` has already started needs it.
- Key members of a set made with `--include-keys` are restored only with
  `-e novafabric_restore_keys=true`.
- If the restore fails, the service stays stopped so it does not serve half-restored data.
  Re-run with `-e novafabric_no_log=false` to see the `nova` output; it never prints the DSN.

To rebuild a lost host: run `site.yml` on the new host with the same inventory, then
`restore.yml` with `novafabric_restore_force=true`.

## Uninstall

```bash
ansible-playbook -i inventory.yml uninstall.yml
```

Stops and removes the service and backup units and the virtualenv. The data directory,
the backup directory and the user are always kept; `/etc/novafabric` too, unless
`-e novafabric_uninstall_remove_config=true`. Delete the data by hand, and only once you
hold a verified backup elsewhere.

## Linting locally

```bash
cd deploy/ansible
ansible-lint site.yml restore.yml uninstall.yml roles/novafabric
ansible-playbook -i inventory.example.yml site.yml --syntax-check
```
