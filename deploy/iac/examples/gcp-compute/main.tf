# EXPERIMENTAL: thin GCP wiring of the provider-neutral module. Validated
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
  machine_types = {
    small  = "e2-medium"
    medium = "e2-standard-4"
    large  = "e2-standard-8"
  }

  target_tag = "${var.name}-server"

  # A GCP firewall rule cannot mix IPv4 and IPv6 source ranges, so each neutral
  # rule becomes up to two firewall rules. Empty by default.
  firewall = {
    for item in flatten([
      for rule in module.server.ingress_rules : [
        for family, ranges in {
          v4 = [for c in rule.cidrs : c if !strcontains(c, ":")]
          v6 = [for c in rule.cidrs : c if strcontains(c, ":")]
          } : {
          key    = "${rule.name}-${family}"
          rule   = rule
          ranges = ranges
        } if length(ranges) > 0
      ]
    ]) : item.key => item
  }
}

resource "google_compute_firewall" "ingress" {
  for_each = local.firewall

  name          = "${var.name}-allow-${each.key}"
  description   = each.value.rule.description
  network       = var.network
  direction     = "INGRESS"
  source_ranges = each.value.ranges
  target_tags   = [local.target_tag]

  allow {
    protocol = each.value.rule.protocol
    ports    = [tostring(each.value.rule.port)]
  }
}

resource "google_compute_instance" "server" {
  name         = var.name
  zone         = var.zone
  machine_type = coalesce(var.machine_type, local.machine_types[module.server.sizing.tier])
  tags         = [local.target_tag]
  labels       = module.server.labels

  boot_disk {
    auto_delete = !var.keep_disk_on_destroy

    initialize_params {
      image = var.boot_image
      size  = module.server.sizing.disk_gib
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = var.subnetwork

    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []
      content {}
    }
  }

  metadata = {
    # Rendered by the neutral module; it carries secret NAMES only.
    user-data              = module.server.user_data
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  dynamic "service_account" {
    for_each = var.service_account_email == null ? [] : [var.service_account_email]
    content {
      email = service_account.value
      # Access is governed by the IAM roles granted to this account; grant it
      # only what you need (for example read access to one secret).
      scopes = ["cloud-platform"]
    }
  }
}
