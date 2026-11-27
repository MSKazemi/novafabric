output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.server.id
}

output "private_ip" {
  description = "Private IPv4 address of the instance."
  value       = aws_instance.server.private_ip
}

output "public_ip" {
  description = "Public IPv4 address, or empty when associate_public_ip_address = false."
  value       = aws_instance.server.public_ip
}

output "secret_references" {
  description = "Secret NAMES the operator must resolve into the host's env file (never values)."
  value       = module.server.secret_references
}

output "health_checks" {
  description = "On-host loopback health endpoints."
  value       = module.server.health_checks
}

output "service" {
  description = "Systemd unit, service user, listener and paths on the host."
  value       = module.server.service
}
