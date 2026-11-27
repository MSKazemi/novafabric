# Example: NovaFabric on one AWS EC2 instance

**Status: experimental.** CI runs `tofu init -backend=false` and
`tofu validate` on this example with no credentials. This project has never
applied it. **Cost warning:** `tofu apply` creates billed AWS resources (an EC2
instance, an EBS volume, and a public IPv4 address if you enable one) in an
account you control. Nothing here is free-tier by design; check current
pricing for your region before applying, and destroy what you do not use.

A thin wrapper around the provider-neutral module
[`../../modules/novafabric-single-host`](../../modules/novafabric-single-host);
read [`../../README.md`](../../README.md) for what it renders and why.

## What it creates

| Resource | Default |
|---|---|
| `aws_security_group` | **no ingress rule**; one rule per CIDR you name in `admin_cidrs` (tcp/22) or `client_cidrs` (tcp/443). Egress: tcp/80 and tcp/443 for OS and Python packages (`egress_cidrs`), plus tcp/5432 to `postgres_cidrs` with `backend = "postgres"` |
| `aws_instance` | Ubuntu 24.04 AMI you pin (`ami_id`), in an **existing** VPC/subnet, **no public IP**, **no instance profile**, IMDSv2 required (hop limit 1), encrypted gp3 root volume sized by `disk_gib`; `user_data` from the neutral module |

Size tiers map to `t3.medium` (small), `m7i.xlarge` (medium) and `m7i.2xlarge`
(large); `instance_type` overrides them.

It creates no VPC, subnet, database, secret, DNS record, certificate, IAM role
or load balancer. Bring those yourself, or leave them out.

## Use

```bash
cd deploy/iac/examples/aws-ec2
cp terraform.tfvars.example terraform.tfvars   # git-ignored; placeholders only
tofu init
tofu plan     # reads your account with YOUR credentials; creates nothing
tofu apply    # creates billed resources
```

Choose a state backend for yourself (none is configured here) and protect it:
the state holds the rendered `user_data` (no secret values) and resource IDs.

Without a public IP the instance needs a route to the Ubuntu archive and the
Python package index (for example a NAT gateway, or a mirror set in
`package_index_url`) for the first boot to install NovaFabric.

With `backend = "postgres"`, write the DSN from the secret you named in
`postgres_dsn_secret_ref` into `/etc/novafabric/server.env` on the instance
and start the service, as the
[operator guide](../../../../docs/ops/infrastructure-as-code.md) describes. With
no instance profile the instance cannot read AWS Secrets Manager itself; that
is the default on purpose.

## Teardown

`tofu destroy` deletes the instance **and its root volume, with every capsule
on it**, unless `keep_disk_on_destroy = true` (a kept volume keeps billing).
Back up first: [backup and restore](../../../../docs/ops/backup-restore.md).
