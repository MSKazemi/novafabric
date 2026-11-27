# Troubleshooting

Symptom → check → fix, for a local install and the `nova serve` dashboard. Each
row quotes the message the code prints, so you can search this page for the text
on your screen. Every check runs offline, and none of them sends anything anywhere.

Status labels follow the [docs honesty rule](../../CONTRIBUTING.md#documentation-status-labels).
`nova doctor` **works today**. `nova serve`, `nova support-bundle` and `nova seal init`
are **experimental**. For a `nova server` deployment (server mode, `.server-token`,
OIDC, `/readyz`), use the [incident runbook](incident-runbook.md). It covers the
server-side failures this page leaves out.

## First moves

```bash
nova doctor --check-extras --check-storage   # extras installed? storage backend healthy?
curl -s http://127.0.0.1:4321/api/health     # only if `nova serve` should be running
```

`nova doctor` with no flag prints only a hint. Pass at least one `--check-*` flag.
`--check-extras` never changes the exit code. `--check-storage` exits `1` when the
backend cannot be opened, and prints the error.

---

## Server unreachable (dashboard will not connect)

The dashboard's Connect panel shows `Could not connect: …`, or `curl` reports
`Connection refused`. The panel's default URL is `http://127.0.0.1:4321`, which is
`nova serve`'s default `--host` and `--port`.

| Symptom | Check | Fix |
|---|---|---|
| `nova serve` prints a banner and exits without listening | You left out `--experimental`, which the command requires | `nova serve --experimental` |
| **The `serve` extra is not installed.** | `nova doctor --check-extras` shows `✗ serve` | `pip install 'novafabric[serve]'` (adds FastAPI and uvicorn) |
| `Refusing to bind to <host> without --insecure.` (exit 2) | `--host` is not a loopback address | Keep the default `127.0.0.1`. Pass `--insecure` only when you really need another interface, and put TLS in front of it |
| `Connection refused` on `127.0.0.1:4321` | Is the process running? Does its `Listening:` line show the URL you typed? | Start it, or pass the URL it printed. If the port is taken, start it with `--port <n>` and use that port in the Connect panel |
| HTTP 403 `{"error": "host_not_localhost", …}` | You reached the server under a name other than `127.0.0.1`, `localhost` or `[::1]`. Every request with another `Host` header is refused as a DNS-rebinding defence | Open it as `http://127.0.0.1:<port>`. For an SSH tunnel, forward to a local port and browse to `localhost` |

`/api/health`, `/livez` and `/readyz` need no token. If `curl` gets JSON from one of
them, the server is up and the problem is the token (next section).

---

## Token rejected (HTTP 401)

The dashboard shows `Could not connect: token rejected`. The API answers 401 with
`{"detail": "missing or invalid token"}`.

`nova serve` chooses its token in this order: `NOVAFABRIC_SERVE_TOKEN` in the
server's environment, then the token stored in `$NOVAFABRIC_HOME/.serve-token`
(`~/.novafabric/.serve-token` when the variable is unset), then a new random token.
It writes the result to that file (mode 0600) and keeps it across restarts. The
startup panel prints the dashboard URL with the token, plus the line
`Token also written to <path> (mode 0600).`

| Symptom | Check | Fix |
|---|---|---|
| 401 with a token copied from the file | Is the path in `Token also written to …` the file you read? A different `NOVAFABRIC_HOME` in your shell means a different file | Read the file the server named, or copy the dashboard URL from the startup panel |
| 401 after setting or changing `NOVAFABRIC_SERVE_TOKEN` | The variable takes priority over the file and replaces the file's contents at startup | Use the value of the variable from the server's environment |
| 401 with a token issued by `POST /api/admin/tokens` | The token may have been revoked | Issue a new one from a session that holds `admin` |
| HTTP 403 `scope '<x>' required for <METHOD> <path>; credential holds '<y>'` | The token is valid, but its scope is too low for that action | Use a token with the required scope. The `.serve-token` holds `admin` |

Never paste the token into a ticket, chat or screenshot. `nova support-bundle` never
reads it.

---

## Postgres DSN errors

The local default is SQLite, which needs no DSN. These messages appear only when the
Postgres backend is selected, with `--backend postgres` or `NOVAFABRIC_BACKEND=postgres`.

| Message | Cause | Fix |
|---|---|---|
| `Postgres backend selected but no DSN provided. Set NOVAFABRIC_POSTGRES_DSN or pass --postgres-dsn.` | The backend is Postgres and no DSN is set | `export NOVAFABRIC_POSTGRES_DSN='postgresql://<user>@<host>:5432/<db>'` |
| `Postgres backend requires 'novafabric[server]'. Run: pip install 'novafabric[server]'` | The Postgres driver (psycopg) is not installed | `pip install 'novafabric[server]'` |
| `Unknown backend: '<x>'. Choose 'sqlite' or 'postgres'.` | Typo in `NOVAFABRIC_BACKEND` or `--backend` | Set it to `sqlite` or `postgres` |
| A driver error such as `connection to server at … failed` | Host, port, password or database name is wrong, or the database is down | Test with `nova doctor --check-storage --backend postgres`, which reads `NOVAFABRIC_POSTGRES_DSN` |
| `postgres_dsn:` in `server.yaml` has no effect | The server config drops secret keys from the YAML on purpose. The DSN is read from the environment only | Set `NOVAFABRIC_POSTGRES_DSN` in the server's environment |

Prefer the environment variable to `--postgres-dsn`: a password on the command line
ends up in shell history and process listings. The driver's error text can include
the database host and user name. `nova support-bundle` redacts the password and keeps
the host, so review `doctor.json` before you share a bundle.

---

## Missing optional extras

NovaFabric splits optional features into extras. A command that needs an extra you
have not installed says so and names the install command, for example
`install the SPKG extra: pip install 'novafabric[spkg]'`. Otherwise it fails with an
`ImportError` or `ModuleNotFoundError`.

| Symptom | Check | Fix |
|---|---|---|
| A command names an extra, or fails on import | `nova doctor --check-extras` lists each extra as `✓` (fully installed) or `✗` with the missing distributions | `pip install 'novafabric[<extra>]'`. Keep the quotes: zsh and some other shells treat `[` `]` as a glob |
| `No extras declared in the installed metadata.` | You are running from a source checkout, not an installed distribution | `uv sync --all-extras` in the checkout |

A missing extra is not an error by itself. Most installs need only a few extras, so
`--check-extras` reports without failing. A support bundle records the installed
extras in `versions.json`.

---

## NovaSeal key or config missing

Sealing is opt-in. NovaFabric reads the signing config from `NOVAFABRIC_SEAL_CONFIG`
if set, else from `$NOVAFABRIC_HOME/novaseal.yaml`. Without either, sealing is off
and `nova capture` writes unsealed capsules **without a warning**. When the config
exists but is broken, capture prints `[novafabric] ⚠ NovaSeal config error: …` on
stderr. It still writes the capsule, unsealed: a sealing failure never blocks the
workload.

| Message | Cause | Fix |
|---|---|---|
| No seal on new capsules, no message; `/api/health` returns `"novaseal_configured": false` | No NovaSeal config was found | `nova seal init` creates a local, self-asserted identity and `novaseal.yaml`, offline |
| `NOVAFABRIC_SEAL_CONFIG points to missing file: <path>` | The variable names a file that does not exist | Correct the path, or unset the variable to use `$NOVAFABRIC_HOME/novaseal.yaml` |
| `NovaSeal key_path not found: <path>` or `NovaSeal cert_path not found: <path>` | The config points at a key or certificate that was moved or deleted | Restore the file from backup. If `nova seal init` wrote the config, running `nova seal init` again reports that it does not load and names `nova seal init --force`. That command archives what is left and issues a new key from the same local CA |
| `the local seal CA (…) is missing or unreadable, so a new leaf cannot be issued under it.` | Rotation needs the CA private key, which is gone too | Restore the CA, or run `nova seal init --force --new-ca`. Verifiers who pinned the old CA will not validate new capsules |
| `novaseal.yaml missing required key: '<key>'` or `local profile requires key_path and cert_path` | The config is incomplete | Complete it. A config that `nova seal init` did not write is never replaced, not even with `--force` |

Check a new capsule with `nova verify <capsule-dir>`. A seal shows that the holder of
the key signed the capsule. It does not show who that holder is.

---

## Support bundles: what is collected and what to review

**Status: experimental** ([ADR-0187](../decisions.md)). Run it when you need help
from someone else:

```bash
nova support-bundle                              # ./nova-support-bundle-<timestamp>.tar.gz
nova support-bundle -o diag.tar.gz --log-window-hours 6
```

The archive is written to the current directory, or to `-o`, and goes nowhere else.
Nothing is uploaded, and the dashboard can only copy the command text. Inside the
archive, `MANIFEST.txt` lists every file and why it is there. The
[CLI reference](../cli-reference.md#nova-support-bundle-experimental-adr-0187) has the
same table.

What is done for you:

- Only allowlisted files are included. Environment variables appear by name only,
  and no collector reads tokens, keys, capsule payloads, prompts or responses.
- Values under secret-like keys, known credential shapes and URL passwords are
  replaced with `[REDACTED]`.
- Your home directory becomes `~`, your user name inside a path becomes `[USER]`,
  and this computer's host name becomes `[HOSTNAME]`. Archive entries carry no owner
  name.

What you still need to check, because redaction is pattern-based and is not a guarantee:

1. `tar -tzvf <bundle>.tar.gz`. Are these the files you expected?
2. Extract the archive into an empty directory and read `MANIFEST.txt`.
3. Read every `logs/*.log` file. Logs are collected only if you keep them in
   `NOVAFABRIC_LOG_DIR` or `$NOVAFABRIC_HOME/logs`, and they contain whatever the
   program that wrote them logged, which can include prompts, responses or personal
   data. Delete anything you do not want to share.
4. Look for remote host names, IP addresses and database user names in
   `doctor.json` and `config.redacted.yaml`. They are kept on purpose because they are
   often the diagnosis. Remove them if your site treats them as confidential.

A support bundle is troubleshooting material. It is not a Run Capsule or an Evidence
Bundle, and it does not prove that anything was verified.
