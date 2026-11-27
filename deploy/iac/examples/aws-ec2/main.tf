# EXPERIMENTAL: thin AWS wiring of the provider-neutral module. Validated
# offline in CI; never applied by this project. See README.md for costs.

module "server" {
  source = "../../modules/novafabric-single-host"

  name                    = var.name
  novafabric_version      = var.novafabric_version
  package_index_url       = var.package_index_url
  backend                 = var.backend
  postgres_dsn_secret_ref = var.postgres_dsn_secret_ref
  workers                 = var.workers
  admin_cidrs             = var.admin_cidrs
  client_cidrs            = var.client_cidrs
  allow_public_https      = var.allow_public_https
  public_hostname         = var.public_hostname
  instance_size           = var.instance_size
  disk_gib                = var.disk_gib
  oidc_issuer_url         = var.oidc_issuer_url
  oidc_audience           = var.oidc_audience
  labels                  = var.labels
}

locals {
  instance_types = {
    small  = "t3.medium"
    medium = "m7i.xlarge"
    large  = "m7i.2xlarge"
  }

  # One security-group rule per (neutral rule, CIDR). Empty by default.
  ingress = {
    for item in flatten([
      for rule in module.server.ingress_rules : [
        for cidr in rule.cidrs : {
          key  = "${rule.name}-${cidr}"
          rule = rule
          cidr = cidr
        }
      ]
    ]) : item.key => item
  }

  egress = merge(
    {
      for item in setproduct(var.egress_cidrs, [80, 443]) :
      "packages-${item[1]}-${item[0]}" => { cidr = item[0], port = item[1] }
    },
    {
      for cidr in var.postgres_cidrs :
      "postgres-${cidr}" => { cidr = cidr, port = 5432 } if var.backend == "postgres"
    },
  )

  tags = merge(module.server.labels, { Name = var.name })
}

resource "aws_security_group" "server" {
  name_prefix = "${var.name}-"
  description = "NovaFabric single host (experimental). No ingress unless CIDRs are named."
  vpc_id      = var.vpc_id
  tags        = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "server" {
  for_each = local.ingress

  security_group_id = aws_security_group.server.id
  description       = each.value.rule.description
  ip_protocol       = each.value.rule.protocol
  from_port         = each.value.rule.port
  to_port           = each.value.rule.port
  cidr_ipv4         = strcontains(each.value.cidr, ":") ? null : each.value.cidr
  cidr_ipv6         = strcontains(each.value.cidr, ":") ? each.value.cidr : null
  tags              = local.tags
}

resource "aws_vpc_security_group_egress_rule" "server" {
  for_each = local.egress

  security_group_id = aws_security_group.server.id
  description       = "outbound tcp/${each.value.port}"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = strcontains(each.value.cidr, ":") ? null : each.value.cidr
  cidr_ipv6         = strcontains(each.value.cidr, ":") ? each.value.cidr : null
  tags              = local.tags
}

resource "aws_instance" "server" {
  ami                         = var.ami_id
  instance_type               = coalesce(var.instance_type, local.instance_types[module.server.sizing.tier])
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.server.id]
  associate_public_ip_address = var.associate_public_ip_address
  key_name                    = var.key_name
  iam_instance_profile        = var.iam_instance_profile

  # Rendered by the neutral module; it carries secret NAMES only.
  user_data                   = module.server.user_data
  user_data_replace_on_change = true

  # IMDSv2 only, and not reachable from containers one hop further away.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_size           = module.server.sizing.disk_gib
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = !var.keep_disk_on_destroy
    tags                  = local.tags
  }

  tags = local.tags
}
