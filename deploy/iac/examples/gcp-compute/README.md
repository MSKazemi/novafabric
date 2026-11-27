# Example: NovaFabric on one Google Compute Engine VM

**Status: experimental.** CI runs `tofu init -backend=false` and
`tofu validate` on this example with no credentials. This project has never
applied it. **Cost warning:** `tofu apply` creates billed Google Cloud
resources (a VM, a persistent disk, and an external IPv4 address if you enable
one) in a project you control. Check current pricing for your region before
applying, and destroy what you do not use.

A thin wrapper around the provider-neutral module
[`../../modules/novafabric-single-host`](../../modules/novafabric-single-host);
read [`../../README.md`](../../README.md) for what it renders and why.

## What it creates

| Resource | Default |
|---|---|
| `google_compute_firewall` | **none**; one INGRESS rule per IP family for the CIDRs you name in `admin_cidrs` (tcp/22) or `client_cidrs` (tcp/443), targeted at the VM's network tag |
| `google_compute_instance` | Ubuntu 24.04 LTS (`boot_image`, the public image family by default; pin an exact image for reproducible rebuilds), in an **existing** network/subnetwork, **no external IP**, **no service account**, Shielded VM (secure boot, vTPM, integrity monitoring), OS Login on, project-wide SSH keys blocked, serial port off, `pd-balanced` boot disk sized by `disk_gib`; `user-data` metadata from the neutral module |

Size tiers map to `e2-medium` (small), `e2-standard-4` (medium) and
`e2-standard-8` (large); `machine_type` overrides them.

It creates no network, Cloud SQL instance, secret, DNS record, certificate,
service account or load balancer. Egress uses your network's own rules.

## Use

```bash
cd deploy/iac/examples/gcp-compute
cp terraform.tfvars.example terraform.tfvars   # git-ignored; placeholders only
tofu init
tofu plan     # reads your project with YOUR credentials; creates nothing
tofu apply    # creates billed resources
```

Choose a state backend for yourself (none is configured here) and protect it:
the state holds the rendered `user-data` (no secret values) and resource IDs.

Without an external IP the VM needs a route to the Ubuntu archive and the
Python package index (for example Cloud NAT, or a mirror set in
`package_index_url`) for the first boot to install NovaFabric.

With `backend = "postgres"`, write the DSN from the secret you named in
`postgres_dsn_secret_ref` into `/etc/novafabric/server.env` on the VM and start
the service, as the
[operator guide](../../../../docs/ops/infrastructure-as-code.md) describes.

## Teardown

`tofu destroy` deletes the VM **and its boot disk, with every capsule on it**,
unless `keep_disk_on_destroy = true` (a kept disk keeps billing). Back up
first: [backup and restore](../../../../docs/ops/backup-restore.md).
