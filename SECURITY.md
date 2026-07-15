# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Use GitHub's [private vulnerability reporting](https://github.com/novafabric/novafabric/security/advisories/new) to report issues confidentially.

We will acknowledge reports within 5 business days and aim to release a fix within 30 days for confirmed vulnerabilities.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Scope

NovaFabric is a self-contained CLI tool. The primary attack surfaces are:

- YAML parsing (malicious spec files)
- SQLite database access (local filesystem)
- CLI argument handling
