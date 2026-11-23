# Release-gate fixtures (issue #218)

Synthetic, `FROM scratch` images that need no QEMU and carry no real software. Each is a
Debian 12 dpkg database with one `libexpat1` record; the trivy DB flags
`2.5.0-1` as CVE-2024-45491 / CVE-2024-45492 (CRITICAL, fixed in `2.5.0-1+deb12u1`).

| fixture | linux/amd64 | linux/arm64 | the gate must |
|---|---|---|---|
| `clean` | fixed version | fixed version | pass |
| `vulnerable` | vulnerable | vulnerable | fail |
| `mixed` | fixed version | **vulnerable** | fail (a host-platform-only scan passes it) |

Used by the `image-gate` job in `.github/workflows/release-toolchain.yml`. The verdict
depends on the trivy vulnerability DB, so a CVE that is ever withdrawn from it would turn
`vulnerable`/`mixed` green: that job asserts the findings exist before it asserts the gate.
