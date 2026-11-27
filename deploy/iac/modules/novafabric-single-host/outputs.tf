# Outputs of the provider-neutral single-host contract. Provider roots consume
# these; none of them contains a secret value.

output "user_data" {
  description = "cloud-init user-data (#cloud-config) for an Ubuntu 24.04 LTS host. Contains no secret values, only secret names."
  value       = local.user_data

  precondition {
    condition     = var.workers == 1 || var.backend == "postgres"
    error_message = "workers > 1 requires backend = \"postgres\": several processes cannot safely share one SQLite file."
  }

  precondition {
    condition     = var.backend != "postgres" || var.postgres_dsn_secret_ref != null
    error_message = "backend = \"postgres\" requires postgres_dsn_secret_ref (the NAME of the secret holding the DSN)."
  }

  precondition {
    condition     = (var.oidc_issuer_url == null) == (var.oidc_audience == null)
    error_message = "set oidc_issuer_url and oidc_audience together, or neither (OIDC is on only when both are set)."
  }
}

output "user_data_sha256" {
  description = "SHA-256 of user_data, for change review and drift checks."
  value       = sha256(local.user_data)
}

output "ingress_rules" {
  description = "Ingress the host needs, as provider-neutral intent: list of {name, protocol, port, cidrs, description}. Empty by default. The NovaFabric API port itself is never listed - it binds loopback only."
  value       = local.ingress_rules

  precondition {
    condition     = var.allow_public_https || alltrue([for c in var.client_cidrs : !can(regex("/0$", c))])
    error_message = "client_cidrs contains a /0 range (HTTPS from anywhere). Set allow_public_https = true to accept that explicitly."
  }
}

output "sizing" {
  description = "Provider-neutral size: {tier, vcpus, memory_gib, disk_gib}. Unbenchmarked pilot starting points."
  value = merge(local.size_tiers[var.instance_size], {
    tier     = var.instance_size
    disk_gib = var.disk_gib
  })
}

output "health_checks" {
  description = "On-host health endpoints. They are on the loopback listener; check them over SSH/session access or through the reverse proxy."
  value = {
    liveness  = "http://${local.listen_host}:${var.server_port}/livez"
    readiness = "http://${local.listen_host}:${var.server_port}/readyz"
  }
}

output "service" {
  description = "What the host runs: systemd unit, service user, loopback listener, state and config paths."
  value = {
    unit        = "novafabric.service"
    user        = local.service_user
    listen      = "${local.listen_host}:${var.server_port}"
    backend     = var.backend
    state_dir   = local.state_dir
    config_file = "${local.config_dir}/server.yaml"
    env_file    = local.env_file
    token_file  = "${local.state_dir}/home/.server-token"
  }
}

output "secret_references" {
  description = "Secret NAMES keyed by the environment variable they must populate in the env file. Never values."
  value       = local.secret_references
}

output "labels" {
  description = "Labels/tags for every resource a provider root creates (defaults merged with var.labels)."
  value       = local.labels
}

output "os_image" {
  description = "Host OS the rendered user-data targets. Provider roots must pass an image of this family; the operator pins the exact image ID."
  value = {
    family         = "ubuntu-24.04-lts"
    python_minimum = "3.12"
    note           = "Ubuntu 24.04 ships Python 3.12, the minimum NovaFabric supports, and cloud-init >= 22.1 (needed for write_files defer)."
  }
}
