# deploy/iac: OpenTofu templates for a single NovaFabric server host

**Status: experimental.** These templates are validated and contract-tested
offline in CI (`.github/workflows/iac-validate.yml`). This project has never
applied them, and the cloud-init they render has not been boot-tested on a
real VM. Treat them as a reviewed starting point for a pilot, not a production
installer. Operator guide: [docs/ops/infrastructure-as-code.md](../../docs/ops/infrastructure-as-code.md).

They are written in HCL for OpenTofu. Every root declares
`required_version = ">= 1.6.0"`, but only OpenTofu 1.13.10 (MPL-2.0, pinned in
CI) is exercised.

```text
deploy/iac/
  modules/novafabric-single-host/   provider-neutral: renders inputs, creates nothing
    tests/contract.tftest.hcl       offline contract tests (plan-only `tofu test`)
    templates/                      cloud-init pieces: installer, systemd unit, env example, reverse-proxy examples
  examples/aws-ec2/                 EXPERIMENTAL thin AWS wiring (one EC2 instance + security group)
  examples/gcp-compute/             EXPERIMENTAL thin GCP wiring (one VM + firewall rules)
```

There is no Azure example. Kubernetes deployments use the
[Helm chart](../helm/novafabric/README.md) instead.

## What the neutral module does

`modules/novafabric-single-host` declares **no provider and no resource**. It
turns a few inputs into values that a provider root wires into its own
instance, firewall and disk:

| Output | What it is |
|---|---|
| `user_data` | `#cloud-config` for **Ubuntu 24.04 LTS**: creates the `novafabric` system user, installs one exact `novafabric[server]==X.Y.Z` into `/opt/novafabric/venv`, writes `/etc/novafabric/server.yaml`, a hardened `novafabric.service` systemd unit and an env-file example, and puts TLS reverse-proxy examples (Caddy, nginx) in `/etc/novafabric/reverse-proxy/`, without installing or enabling a proxy |
| `ingress_rules` | Ingress intent `{name, protocol, port, cidrs, description}`: tcp/22 from `admin_cidrs`, tcp/443 from `client_cidrs`. **Empty by default** |
| `sizing` | `{tier, vcpus, memory_gib, disk_gib}` for `small`, `medium` or `large` (unbenchmarked pilot starting points) |
| `health_checks` | `http://127.0.0.1:<port>/livez` and `/readyz` (on the host only) |
| `service` | Unit name, service user, loopback listener, state, config, env and token paths |
| `secret_references` | Secret **names** keyed by the environment variable they must fill. Never values |
| `labels` | `app`, `managed-by`, `novafabric-version` plus your `labels` |
| `os_image` | The image family the user-data targets |

### Secure defaults

- The API listens on **127.0.0.1** only (`nova server start --host 127.0.0.1`).
  Its port is never opened in a firewall; a reverse proxy on the same host
  terminates TLS and forwards to it.
- **No ingress at all** until you name CIDRs. A `/0` range is refused for SSH,
  and for HTTPS unless you set `allow_public_https = true`.
- **No secret values as inputs.** With `backend = "postgres"` you pass
  `postgres_dsn_secret_ref`, the *name* of the secret in your secret store, and
  a pasted `postgresql://…` string is rejected. The DSN goes into
  `/etc/novafabric/server.env` (root, 0600) on the host, which you write; the
  unit is skipped until that file exists. So the DSN never reaches user-data,
  plans or state through these templates.
- An **exact release** is required (`novafabric_version = "X.Y.Z"`), and pip
  runs with `--disable-pip-version-check`, so no update check is made from the
  host. `package_index_url` points pip at a mirror for air-gapped sites.
- The systemd unit runs as the unprivileged `novafabric` user with
  `ProtectSystem=strict`, `NoNewPrivileges`, an empty capability set and one
  writable path, `/var/lib/novafabric`.
- The provider examples attach **no cloud identity** by default (no instance
  profile, no service account), use **no public IP** by default, require IMDSv2
  and an encrypted root volume (AWS), or enable Shielded VM and OS Login and
  block project-wide SSH keys (GCP).

### Backends

- `sqlite` (default): no external service. Without OIDC the server generates a
  local bearer token at `/var/lib/novafabric/home/.server-token` (mode 0600).
- `postgres`: an **existing** PostgreSQL service you operate. Nothing here
  provisions a database. `workers > 1` requires it.

An existing object-storage endpoint for capsules is **not** an input yet: the
server-side storage boundary is still being decided (#236, #237).

## Validate offline

No account and no credentials are needed. `tofu init` downloads the pinned
providers for the two examples; the module itself needs nothing.

```bash
tofu fmt -check -recursive deploy/iac
for d in deploy/iac/modules/novafabric-single-host deploy/iac/examples/aws-ec2 deploy/iac/examples/gcp-compute; do
  tofu -chdir="$d" init -backend=false && tofu -chdir="$d" validate
done
tofu -chdir=deploy/iac/modules/novafabric-single-host test   # plan-only contract tests
```

`tests/docs/test_iac_module_contract.py` checks the structure and safety rules
(pinned providers, closed defaults, no secret inputs, no committed state, no
Azure module, a credential-free CI job) without needing `tofu`.

## Not committed

`.terraform/`, `.terraform.lock.hcl`, state, plans and real `*.tfvars` files
are git-ignored. Lock files are not committed because their hashes depend on
the registry (OpenTofu or Terraform) and on your platforms. Run
`tofu providers lock` for the platforms you deploy from and keep that lock file
with your own configuration.
