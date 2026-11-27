# Infrastructure as code: one NovaFabric server host (OpenTofu)

`deploy/iac/` holds OpenTofu templates that render everything a single Linux
host needs to run the NovaFabric server (`nova server start`), plus two thin
provider examples. This page is the operator view; the template reference is
[deploy/iac/README.md](../../deploy/iac/README.md).

| Piece | Status |
|---|---|
| Provider-neutral module `deploy/iac/modules/novafabric-single-host` | **experimental**: format, validation and plan-only contract tests run offline in CI on every change to `deploy/iac/`; it has never been applied by this project, and its cloud-init has not been boot-tested on a real VM |
| AWS EC2 example `deploy/iac/examples/aws-ec2` | **experimental**: `init -backend=false` and `validate` in CI only; never applied |
| GCP Compute Engine example `deploy/iac/examples/gcp-compute` | **experimental**: `init -backend=false` and `validate` in CI only; never applied |
| Azure | not provided |
| Existing object-storage endpoint for capsules | **future design**: waits on the server storage boundary (#236, #237) |
| Kubernetes | use the [Helm chart](../../deploy/helm/novafabric/README.md) instead (see [Server Deployment](server-deployment.md)) |

The server itself is **experimental and single-tenant** whatever installs it.
Nothing here is high availability, a managed service or a production
certification.

## What a host gets

The module's `user_data` output is a `#cloud-config` document for Ubuntu 24.04
LTS (Python 3.12, the minimum NovaFabric supports). On first boot it:

1. creates the `novafabric` system user (no login shell);
2. installs one exact release, `novafabric[server]==X.Y.Z`, into
   `/opt/novafabric/venv`, from the default index or your `package_index_url`
   mirror, with pip's update check turned off;
3. writes `/etc/novafabric/server.yaml` (backend, `host: 127.0.0.1`, port, and
   the `oidc:` block when you set an issuer and audience; never a secret);
4. writes and enables `novafabric.service`, which runs
   `nova db upgrade --backend <backend>` and then
   `nova server start --host 127.0.0.1` as `novafabric`, with
   `/var/lib/novafabric` as its only writable path;
5. writes `/etc/novafabric/server.env.example` and Caddy/nginx examples in
   `/etc/novafabric/reverse-proxy/`. **TLS is yours:** no proxy is installed,
   no certificate is requested, and the API port is never opened.

## Secure defaults

- **Nothing is reachable from outside** until you set `admin_cidrs` (SSH) or
  `client_cidrs` (HTTPS to your reverse proxy). A `/0` range is refused for
  SSH, and for HTTPS unless you set `allow_public_https = true`.
- **Secrets by name, never by value.** `postgres_dsn_secret_ref` takes the
  name of a secret in your store; a pasted `postgresql://…` value is rejected
  at plan time. You write the DSN on the host, so it stays out of user-data,
  plans and OpenTofu state.
- **No cloud identity by default.** The AWS example attaches no instance
  profile and the GCP example no service account. Grant one yourself, scoped
  to what the host must read.
- **No public IP by default** in either example.

## Validate offline

Requires only the `tofu` binary (CI pins OpenTofu 1.13.10). No account,
credential or cloud CLI:

```bash
tofu fmt -check -recursive deploy/iac
tofu -chdir=deploy/iac/modules/novafabric-single-host init -backend=false
tofu -chdir=deploy/iac/modules/novafabric-single-host validate
tofu -chdir=deploy/iac/modules/novafabric-single-host test
```

`tofu test` runs the plan-only contract tests in
`modules/novafabric-single-host/tests/`: closed defaults, a loopback-only
listener, the pinned installer, and the refusals (an SSH `/0`, a public HTTPS
range without the opt-in, a DSN pasted as a secret name, `workers > 1` on
SQLite, a moving version such as `latest`, a plain-http index, OIDC with only
one of its two keys). The `iac-validate` workflow runs the same commands, plus
`init -backend=false` and `validate` for both examples.

## Deploy with a provider example

**Cost warning:** applying an example creates billed resources in your account
or project. Read the example's README first:
[AWS](../../deploy/iac/examples/aws-ec2/README.md) ·
[GCP](../../deploy/iac/examples/gcp-compute/README.md).

1. Copy `terraform.tfvars.example` to `terraform.tfvars` (git-ignored) and fill
   in placeholders: region, existing network, image and `novafabric_version`.
2. Configure a state backend you trust; the examples ship none.
3. `tofu init`, `tofu plan` (reads your account with your credentials), then
   `tofu apply`.

### After first boot

On the host (SSH or your provider's session access):

```bash
sudo systemctl status novafabric
curl -fsS http://127.0.0.1:7433/livez
curl -fsS http://127.0.0.1:7433/readyz
sudo cat /var/lib/novafabric/home/.server-token   # SQLite without OIDC: the local bearer token
```

With `backend = "postgres"` the unit is **skipped** until the env file exists.
Write the DSN from your secret store and start it:

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/novafabric/server.env
sudoedit /etc/novafabric/server.env   # NOVAFABRIC_POSTGRES_DSN=… and NOVAFABRIC_METADATA_DSN=…
sudo systemctl start novafabric
```

Then install a reverse proxy, adapt one of the examples in
`/etc/novafabric/reverse-proxy/` and provision its certificate. Server
configuration, authentication and roles are covered in
[Server Deployment](server-deployment.md) and the
[Server admin guide](server-admin-guide.md).

### Upgrade

Changing `novafabric_version` changes `user_data`. The AWS example then
**replaces the instance and, by default, its disk**
(`user_data_replace_on_change`). The GCP example updates the VM's metadata in
place, and cloud-init does not run again on an existing VM, so nothing is
upgraded. Back up first in either case, or upgrade in place on the host with
`/opt/novafabric/venv/bin/python -m pip install 'novafabric[server]==X.Y.Z'`
and a service restart; read the [upgrade guide](upgrade-guide.md) either way.

## Teardown

`tofu destroy` removes what the example created. By default that includes the
disk and **every capsule on it** (`keep_disk_on_destroy = true` keeps it, and a
kept disk keeps billing). Take and verify a backup first:
[Backup and restore](backup-restore.md). An existing PostgreSQL service is
never touched.

## Limits of this slice

- No container-image install method yet; the host installs the PyPI release.
- No secret-store fetch on the host: you write the env file.
- No object-storage input, managed database, DNS, certificate or load balancer.
- No Azure example.
- Size tiers (`small` 2 vCPU / 4 GiB, `medium` 4 / 16, `large` 8 / 32) are
  unbenchmarked starting points; see the [sizing guide](sizing-guide.md).
