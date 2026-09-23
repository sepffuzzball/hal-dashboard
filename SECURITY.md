# Security Policy

## Supported Versions

HAL Dashboard is a single-operator, single-machine service tracked on one
branch. Only the current `main` branch is supported with security fixes;
older tags and unofficial forks are not patched separately. Apply fixes by
updating the deployment and restarting the service.

## Reporting a Vulnerability

Please report security vulnerabilities **privately** through GitHub Security
Advisories on this repository:

<https://github.com/sepffuzzball/hal-dashboard/security/advisories/new>

Do **not** file public issues for unfixed vulnerabilities - the dashboard is
exposed on a LAN through a reverse proxy and a public write-up can be an
exploit map before a fix ships.

Response expectations: reports are reviewed on a best-effort basis, typically
within a few business days. This is a personal project without a formal
security team, so no strict SLA is promised - but valid reports are
acknowledged and fixed in `main` when feasible, and you may be credited in the
advisory with your permission.

## Context and Cautions

- **The dashboard controls systemd units.** A holder of the admin token can
  start/stop the configured LLM and ComfyUI services (via a least-privilege
  polkit grant limited to those exact units and the `start`/`stop` verbs). A
  token leak therefore means control of those services plus all status data -
  there are no users or roles, only one shared token.
- **Redact tokens in logs and screenshots.** The admin token is sent in the
  `X-Hal-Token` header; proxy access logs, debug logs, and screenshots (this
  repository includes a UI preview) can leak it or reveal environment paths.
  Rotate immediately if a token may have leaked (edit the root-owned env file
  and restart the service).
- The API enforces token authentication, exact-origin checks on mutations, and
  security headers, and it never executes shells or arbitrary units - but TLS is not terminated by the app itself, so keep it behind a reverse proxy when
  it is reachable beyond loopback.
