# Security policy

## Private reporting

Report suspected vulnerabilities through this repository's GitHub **Security →
Advisories → Report a vulnerability** (a private vulnerability report). Include
the affected version, impact, source locations, and a minimal reproduction using
dummy data. Do not include live tokens or personal data.
Do not open a public issue or PR with exploit details before coordination.

If private reporting is unavailable, the repository administrators must enable
it before this reporting route can accept submissions. Do not substitute a
public issue containing vulnerability details. This policy does not promise a
response SLA or a supported-version window; include the affected versions.

## Threat model

Backlogr is a single shared trust domain for cooperating agents and their human
operator. The HTTP server requires `BACKLOGR_TOKEN`; authenticated API and MCP
callers have broad access to the same tasks. There are no per-user roles or
tenant isolation. Actor and approver names are caller-supplied workflow metadata,
not independently authenticated identities. Approval rules help enforce workflow;
they do not protect against a malicious holder of the shared token.

The token is compared with `hmac.compare_digest`. Keep it secret, use mode-600
CLI token files, and restrict access to the database, exports, backups, logs,
and process environment. Local CLI and stdio MCP use filesystem/process access
as their trust boundary; stdio does not require the HTTP bearer token. Trusted
local `import --restore` deliberately bypasses creation gates.

`/healthz` is unauthenticated. `/dashboard` and `/dashboard.json` are read-only
and require the bearer token, unless `BACKLOGR_TRUST_TAILSCALE_HEADERS` is set,
in which case a nonempty `Tailscale-User-Login` header is also accepted. Enable it
only when the listener is reachable solely through `tailscale serve`, which sets
that header; any client that can connect directly could forge it.

Because names are caller-supplied, the approval gate is a workflow guard, not an
access control: with `BACKLOGR_APPROVERS` unset, any token holder can record any
non-bot name as `approved_by`. Set `BACKLOGR_APPROVERS` to narrow it.

Exports and nightly backups are written mode 600. The remote CLI does not follow
HTTP redirects (urllib would forward the bearer token to the new host) and warns
when `BACKLOGR_URL` uses plain HTTP to a non-loopback host.

`BACKLOGR_HOSTS` controls listener addresses. `BACKLOGR_ALLOWED_HOSTS` adds Host
names to the MCP transport's DNS rebinding protection; it is not an IP access
list or an authentication mechanism. `/api` and `/healthz` are handled
outside the MCP Host check. Enforce Host restrictions at the proxy for all routes.

Do not expose Backlogr publicly without TLS and authentication. Prefer loopback
or a private network with a trusted reverse proxy. The built-in HTTP listener
provides no TLS; configure HTTPS and access controls at the proxy, retain bearer
authentication for API/MCP. Keep secrets out of source control. See
[configuration](docs/CONFIGURATION.md) for defaults and
[architecture](docs/ARCHITECTURE.md) for the data and request flow.
