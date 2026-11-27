# Offline contract tests for the provider-neutral module.
#
# Every run is `command = plan` against a module that declares no providers and
# no resources: nothing is created, no account or credential is needed, and no
# network call is made. Run with `tofu test` from the module directory (CI:
# .github/workflows/iac-validate.yml).

variables {
  novafabric_version = "0.109.0"
}

# ---------------------------------------------------------------- defaults ---

run "defaults_expose_nothing_and_bind_loopback" {
  command = plan

  assert {
    condition     = length(output.ingress_rules) == 0
    error_message = "the default configuration must not open any ingress."
  }

  assert {
    condition     = startswith(output.user_data, "#cloud-config\n") && can(yamldecode(output.user_data))
    error_message = "user_data must be a #cloud-config YAML document."
  }

  assert {
    condition     = !strcontains(output.user_data, "0.0.0.0")
    error_message = "the rendered host configuration must never bind or allow 0.0.0.0."
  }

  assert {
    condition = strcontains(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/etc/systemd/system/novafabric.service"]),
      "--host 127.0.0.1 --port 7433",
    )
    error_message = "the systemd unit must start the API on 127.0.0.1:7433."
  }

  assert {
    condition = strcontains(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/usr/local/sbin/install-novafabric"]),
      "\"novafabric[server]==0.109.0\"",
    )
    error_message = "the installer must pin the exact novafabric release."
  }

  assert {
    condition = startswith(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/usr/local/sbin/install-novafabric"]),
      "#!/bin/sh\n",
    )
    error_message = "the installer must start with its shebang."
  }

  assert {
    condition = strcontains(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/usr/local/sbin/install-novafabric"]),
      "--disable-pip-version-check",
    )
    error_message = "the installer must not make an update check."
  }

  assert {
    condition = yamldecode(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/etc/novafabric/server.yaml"])
    ).host == "127.0.0.1"
    error_message = "server.yaml must bind loopback."
  }

  assert {
    condition     = length(output.secret_references) == 0
    error_message = "the SQLite default needs no secret."
  }

  assert {
    condition     = output.service.listen == "127.0.0.1:7433" && output.service.user == "novafabric"
    error_message = "the service must run as the novafabric user on the loopback listener."
  }

  assert {
    condition     = output.sizing.vcpus == 2 && output.sizing.memory_gib == 4 && output.sizing.disk_gib == 50
    error_message = "the small tier is 2 vCPU / 4 GiB with a 50 GiB disk by default."
  }

  assert {
    condition     = output.labels["novafabric-version"] == "0-109-0" && output.labels["managed-by"] == "opentofu"
    error_message = "labels must carry the version in a provider-safe form."
  }
}

# ------------------------------------------------------------ network rules ---

run "ssh_open_to_the_world_is_refused" {
  command = plan
  variables {
    admin_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.admin_cidrs]
}

run "ssh_open_to_the_ipv6_world_is_refused" {
  command = plan
  variables {
    admin_cidrs = ["::/0"]
  }
  expect_failures = [var.admin_cidrs]
}

run "an_invalid_cidr_is_refused" {
  command = plan
  variables {
    admin_cidrs = ["203.0.113.7"]
  }
  expect_failures = [var.admin_cidrs]
}

run "ssh_from_a_named_range_is_rendered" {
  command = plan
  variables {
    admin_cidrs = ["203.0.113.0/24"]
  }

  assert {
    condition     = length(output.ingress_rules) == 1 && output.ingress_rules[0].port == 22 && output.ingress_rules[0].cidrs == tolist(["203.0.113.0/24"])
    error_message = "a named admin range must produce exactly one tcp/22 rule."
  }
}

run "public_https_needs_an_explicit_opt_in" {
  command = plan
  variables {
    client_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [output.ingress_rules]
}

run "public_https_with_the_opt_in" {
  command = plan
  variables {
    client_cidrs       = ["0.0.0.0/0"]
    allow_public_https = true
  }

  assert {
    condition     = length(output.ingress_rules) == 1 && output.ingress_rules[0].port == 443
    error_message = "with the opt-in, the only public rule is tcp/443 to the reverse proxy."
  }
}

# ------------------------------------------------------------------ secrets ---

run "a_dsn_value_is_refused_as_a_secret_reference" {
  command = plan
  variables {
    backend                 = "postgres"
    postgres_dsn_secret_ref = "postgresql://nova:hunter2@db.internal:5432/nova"
  }
  expect_failures = [var.postgres_dsn_secret_ref]
}

run "postgres_requires_a_secret_reference" {
  command = plan
  variables {
    backend = "postgres"
  }
  expect_failures = [output.user_data]
}

run "postgres_with_a_secret_reference" {
  command = plan
  variables {
    backend                 = "postgres"
    postgres_dsn_secret_ref = "novafabric/pilot/postgres-dsn"
    workers                 = 2
  }

  assert {
    condition     = output.secret_references["NOVAFABRIC_POSTGRES_DSN"] == "novafabric/pilot/postgres-dsn" && length(output.secret_references) == 2
    error_message = "the secret NAME must be mapped to both DSN environment variables."
  }

  assert {
    condition = strcontains(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/etc/systemd/system/novafabric.service"]),
      "ConditionPathExists=/etc/novafabric/server.env",
    )
    error_message = "with postgres the unit must wait for the operator-written env file."
  }

  assert {
    condition     = !strcontains(output.user_data, "postgresql://") && !strcontains(output.user_data, "postgres://")
    error_message = "no connection string may appear in user-data."
  }

  assert {
    condition = strcontains(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/etc/systemd/system/novafabric.service"]),
      "--workers 2",
    )
    error_message = "workers must reach nova server start."
  }
}

run "several_workers_need_postgres" {
  command = plan
  variables {
    workers = 4
  }
  expect_failures = [output.user_data]
}

# ------------------------------------------------------------ other inputs ---

run "a_moving_version_is_refused" {
  command = plan
  variables {
    novafabric_version = "latest"
  }
  expect_failures = [var.novafabric_version]
}

run "a_plain_http_package_index_is_refused" {
  command = plan
  variables {
    package_index_url = "http://mirror.internal/simple"
  }
  expect_failures = [var.package_index_url]
}

run "oidc_needs_issuer_and_audience_together" {
  command = plan
  variables {
    oidc_issuer_url = "https://idp.example.com/realms/nova"
  }
  expect_failures = [output.user_data]
}

run "oidc_is_rendered_into_server_yaml" {
  command = plan
  variables {
    oidc_issuer_url = "https://idp.example.com/realms/nova"
    oidc_audience   = "nova-server"
  }

  assert {
    condition = yamldecode(
      one([for f in yamldecode(output.user_data).write_files : f.content if f.path == "/etc/novafabric/server.yaml"])
    ).oidc.audience == "nova-server"
    error_message = "the oidc block must reach server.yaml."
  }
}
