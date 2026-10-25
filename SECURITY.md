# Security policy

## Private reporting

Report suspected vulnerabilities through this repository's GitHub **Security →
Advisories → Report a vulnerability** (a private vulnerability report). Include
the affected version, impact, source locations, and a minimal reproduction using
dummy data. Do not include live tokens, signed action URLs, or personal data.
Do not open a public issue or PR with exploit details before coordination.

If private reporting is unavailable, the repository administrators must enable
it before this reporting route can accept submissions. Do not substitute a
public issue containing vulnerability details. This policy does not promise a
response SLA or a supported-version window; include whether 1.4.0 is affected.

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

`/healthz` is unauthenticated. Optional `/act` action links also bypass the API
bearer token: possession of a valid link grants the encoded action on that task.
Links use a separate `BACKLOGR_LINK_SECRET` with HMAC-SHA256 over a version marker,
task ID, action, expiry, and random nonce. GET displays confirmation; POST applies
the action and consumes its nonce atomically in SQLite. Reuse is rejected.
Question/later text is supplied on confirmation and is not part of the signature.
Links expire; rotating the secret invalidates outstanding links. Treat URLs as
secrets and avoid recording query strings in proxy logs. The configured link actor
may receive human attestation privileges if listed in `BACKLOGR_HUMANS`.

`BACKLOGR_HOSTS` controls listener addresses. `BACKLOGR_ALLOWED_HOSTS` adds Host
names to the MCP transport's DNS rebinding protection; it is not an IP access
list or an authentication mechanism. `/api`, `/act`, and `/healthz` are handled
outside the MCP Host check. Enforce Host restrictions at the proxy for all routes.

Do not expose Backlogr publicly without TLS and authentication. Prefer loopback
or a private network with a trusted reverse proxy. The built-in HTTP listener
provides no TLS; configure HTTPS and access controls at the proxy, retain bearer
authentication for API/MCP, and restrict `/act` access deliberately. Keep secrets
out of source control and use a separate link secret. See
[configuration](docs/CONFIGURATION.md) for defaults and
[architecture](docs/ARCHITECTURE.md) for the data and request flow.
