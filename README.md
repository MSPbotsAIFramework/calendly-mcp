# calendly-mcp

MCP server for [Calendly](https://calendly.com), the scheduling/meeting-booking platform. Lets an
MCP-compatible AI agent (Claude, ChatGPT, Cursor, or any other MCP client) read a connected user's
Calendly event types, check availability, list and manage scheduled meetings, and generate
scheduling links -- all through a stateless, per-tenant Streamable HTTP service.

- **Transport**: MCP Streamable HTTP (`POST /mcp`), stateless -- no session stickiness required.
- **Tech stack**: Python 3.12, [`mcp[cli]`](https://pypi.org/project/mcp/) (FastMCP), `httpx`, `uv`.
- **License**: Private, MSPbots internal use.

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

Every `/mcp` request must carry:

| Header | Required | Meaning | Where to get it |
|---|---|---|---|
| `X-Calendly-Api-Key` | Yes | Calendly Personal Access Token. Sent upstream as `Authorization: Bearer <token>`. | Calendly → **Integrations** → **API & Webhooks** → "Get a token now" |

No OAuth app registration is used: Calendly's API accepts both an OAuth2 bearer token and a
Personal Access Token under the identical `Authorization: Bearer <token>` scheme, and a PAT needs
no redirect/callback surface -- this is a stateless service with none.

Missing the header returns `401` with a structured error naming it; no call is made to Calendly.

## Tools

9 tools, scheduling-only (see "Non-goals" in the ticket's tech design for what was deliberately
left out -- contacts, routing forms, webhooks, team management, and the activity log).

| Tool | What it does | Read/write |
|---|---|---|
| `calendly_get_current_user` | Get the connected account's identity and URIs. | read |
| `calendly_list_event_types` | List the account's meeting types. | read |
| `calendly_get_event_type_available_times` | List open booking slots for one meeting type (≤31-day window). | read |
| `calendly_list_scheduled_events` | List the account's scheduled meetings, newest first. | read |
| `calendly_get_scheduled_event` | Get full detail for one scheduled meeting. | read |
| `calendly_list_event_invitees` | List who is invited to one meeting. | read |
| `calendly_cancel_scheduled_event` | Cancel one meeting by UUID. Destructive. | write |
| `calendly_list_user_busy_times` | List a user's busy blocks (≤7-day window). | read |
| `calendly_create_scheduling_link` | Create a single-use link to share so someone else can book a time. | write |

## Local development

```bash
uv sync --group dev
uv run pytest
uv run python -m calendly_mcp   # starts on 0.0.0.0:8080 (override with MCP_HTTP_HOST/MCP_HTTP_PORT)
```

Manual round trip (same path production traffic takes, with a placeholder credential):

```bash
curl -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'X-Calendly-Api-Key: your_token_here' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl http://localhost:8080/health   # {"status":"ok"}, no credential required
```

## Error envelope

Every tool returns either its result or, on failure, a JSON string:

```json
{"error": {"code": "unauthorized", "message": "...", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`.
