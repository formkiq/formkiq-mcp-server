# FormKiQ MCP Server

MCP server for FormKiQ, exposing the FormKiQ OpenAPI spec and selected FormKiQ API operations as Model Context Protocol tools.

This server:

- exposes the FormKiQ JWT OpenAPI spec as an MCP resource at `openapi://formkiq/jwt`
- parses operation metadata from the OpenAPI document
- proxies a curated set of FormKiQ operations as MCP tools
- forwards calls to a deployed FormKiQ API using a JWT
- defaults to read-only access, with separate opt-in write and purge tools
- validates arguments and streams responses within a configurable size limit

Each MCP server release bundles an OpenAPI snapshot for offline startup. Its upstream source and checksum are recorded in [provenance.json](formkiq_mcp/data/provenance.json). The MCP server version and the upstream FormKiQ API version are independent.

## Quick Start

For release installations, let your MCP client start this server with `pipx` from a GitHub release tag, using `@vMAJOR.MINOR.PATCH`.

The examples below target the planned first release, `v0.1.0`, matching the package version in `pyproject.toml`. Until that tag is published, use the checkout-based [Install](#install) and [Local Codex Development Setup](#local-codex-development-setup) instructions. For later versions, select a published tag from [Releases](https://github.com/formkiq/formkiq-mcp-server/releases).

Use the FormKiQ CloudFormation `HttpApiUrl` output for JWT/Cognito authentication:

```bash
aws cloudformation describe-stacks \
  --stack-name "<formkiq-stack-name>" \
  --query "Stacks[0].Outputs[?OutputKey=='HttpApiUrl'].OutputValue | [0]" \
  --output text
```

Replace the example endpoint and JWT values below. Keep the same release tag across client configurations. The tag selects the application version; the checked-in `uv.lock` pins dependencies for checkout-based installations.

### Codex Setup

Add this to `~/.codex/config.toml` or to a trusted project's `.codex/config.toml`:

```toml
[mcp_servers.formkiq]
command = "pipx"
args = ["run", "--spec", "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.1.0", "formkiq-mcp-server"]
env = { "MCP_TRANSPORT" = "stdio", "FORMKIQ_API_ENDPOINT_URL" = "https://abc123.execute-api.us-east-1.amazonaws.com", "FORMKIQ_JWT" = "eyJ..." }
startup_timeout_sec = 30
tool_timeout_sec = 60
enabled = true
```

Start a new Codex session and check:

```text
/mcp
```

### Claude Code Setup

Add the server from the CLI:

```bash
claude mcp add --transport stdio formkiq \
  --env FORMKIQ_API_ENDPOINT_URL=https://abc123.execute-api.us-east-1.amazonaws.com \
  --env FORMKIQ_JWT=eyJ... \
  --env MCP_TRANSPORT=stdio \
  -- pipx run --spec "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.1.0" formkiq-mcp-server
```

Then check:

```bash
claude mcp list
```

Inside Claude Code, use:

```text
/mcp
```

## Requirements

- Python 3.11+
- `pipx` for GitHub-backed MCP client setup, or `uv` for local development
- a deployed FormKiQ API endpoint URL from the FormKiQ CloudFormation outputs
- a FormKiQ JWT for API calls

Install `pipx` if needed:

```bash
brew install pipx
```

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Install

```bash
git clone https://github.com/formkiq/formkiq-mcp-server.git
cd formkiq-mcp-server
uv sync --locked
```

After the release tag is published, you can also run directly from GitHub with `pipx`:

```bash
pipx run --spec "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.1.0" formkiq-mcp-server
```

## Configure

Set the FormKiQ API endpoint and JWT. For JWT authentication, `FORMKIQ_API_ENDPOINT_URL` should be the `HttpApiUrl` value from the FormKiQ CloudFormation stack outputs.

```bash
export FORMKIQ_API_ENDPOINT_URL="https://abc123.execute-api.us-east-1.amazonaws.com"
export FORMKIQ_JWT="eyJ..."
```

FormKiQ exposes different API endpoint outputs for different authentication modes:

| Authentication mode | CloudFormation output | OpenAPI spec |
|---|---|---|
| JWT / Cognito | `HttpApiUrl` | `openapi-jwt.yaml` |
| IAM | `IamApiUrl` | `openapi-iam.yaml` |
| API Key | `KeyApiUrl` | `openapi-key.yaml` |

This MCP server currently sends bearer JWTs through `FORMKIQ_JWT`. The neutral `FORMKIQ_API_ENDPOINT_URL` name is used so IAM and API key support can be added without renaming the endpoint variable again.

You can retrieve the JWT endpoint, `HttpApiUrl`, with the AWS CLI:

```bash
aws cloudformation describe-stacks \
  --stack-name "<formkiq-stack-name>" \
  --query "Stacks[0].Outputs[?OutputKey=='HttpApiUrl'].OutputValue | [0]" \
  --output text
```

Then export it directly:

```bash
export FORMKIQ_API_ENDPOINT_URL="$(aws cloudformation describe-stacks \
  --stack-name "<formkiq-stack-name>" \
  --query "Stacks[0].Outputs[?OutputKey=='HttpApiUrl'].OutputValue | [0]" \
  --output text)"
```

Changing the endpoint or OpenAPI document alone does not enable IAM signing or API-key authentication. This implementation supports JWT authentication only.

`FORMKIQ_API_BASE_URL` is still accepted as a deprecated alias for compatibility, but new configs should use `FORMKIQ_API_ENDPOINT_URL`.

Optional: override the bundled OpenAPI document with a compatible local file:

```bash
export FORMKIQ_OPENAPI_FILE="/path/to/formkiq-core/docs/openapi/openapi-jwt.yaml"
```

Optional: override the bundled document with an HTTPS URL (pin the URL to an immutable version). Set only one OpenAPI override:

```bash
export FORMKIQ_OPENAPI_URL="https://example.com/openapi-jwt.yaml"
```

## Run Locally Over HTTP

```bash
uv run --locked python server.py
```

Or, after installing/running through `pipx`:

```bash
pipx run --spec "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.1.0" formkiq-mcp-server
```

By default, the Python MCP SDK serves Streamable HTTP at:

```text
http://localhost:8000/mcp
```

Use this mode for MCP clients that connect to an already-running HTTP server.

## Run Over Stdio

Most local AI clients start MCP servers as child processes over stdio. This server supports that mode with:

```bash
MCP_TRANSPORT=stdio uv run --locked python server.py
```

You normally do not run that command manually. Instead, put it in your MCP client configuration.

## Tools

This server exposes a curated allowlist:

- `list_formkiq_operations`
- `call_formkiq_operation` (read/search only)
- `describe_operation` (parameters, schemas, and examples)
- `get_version`
- `get_documents`
- `get_document`
- `search_documents`

Use `list_formkiq_operations` to discover what is enabled for the current caller, then `describe_operation` before constructing arguments. The table below is the supported catalog; write and purge operations are disabled by default. The convenience tools cover common read/search workflows.

### Supported FormKiQ API Endpoints

| Method | Path | Operation ID |
|---|---|---|
| `GET` | `/activities` | `GetResourceActivities` |
| `GET` | `/attributes` | `GetAttributes` |
| `POST` | `/attributes` | `AddAttribute` |
| `GET` | `/attributes/{key}` | `GetAttribute` |
| `PATCH` | `/attributes/{key}` | `UpdateAttribute` |
| `DELETE` | `/attributes/{key}` | `DeleteAttribute` |
| `GET` | `/attributes/{key}/allowedValues` | `GetAttributeAllowedValues` |
| `GET` | `/documents` | `GetDocuments` |
| `POST` | `/documents/upload` | `AddDocumentUpload` |
| `GET` | `/documents/{documentId}` | `GetDocument` |
| `PATCH` | `/documents/{documentId}` | `UpdateDocument` |
| `DELETE` | `/documents/{documentId}` | `DeleteDocument` |
| `GET` | `/documents/{documentId}/actions` | `GetDocumentActions` |
| `POST` | `/documents/{documentId}/actions` | `AddDocumentActions` |
| `GET` | `/documents/{documentId}/artifacts` | `GetDocumentArtifacts` |
| `GET` | `/documents/{documentId}/attributes` | `GetDocumentAttributes` |
| `POST` | `/documents/{documentId}/attributes` | `AddDocumentAttributes` |
| `PUT` | `/documents/{documentId}/attributes` | `SetDocumentAttributes` |
| `GET` | `/documents/{documentId}/attributes/{attributeKey}` | `GetDocumentAttribute` |
| `PUT` | `/documents/{documentId}/attributes/{attributeKey}` | `SetDocumentAttributeValue` |
| `DELETE` | `/documents/{documentId}/attributes/{attributeKey}` | `DeleteDocumentAttribute` |
| `DELETE` | `/documents/{documentId}/attributes/{attributeKey}/{attributeValue}` | `DeleteDocumentAttributeAndValue` |
| `GET` | `/documents/{documentId}/content` | `GetDocumentContent` |
| `DELETE` | `/documents/{documentId}/purge` | `PurgeDocument` |
| `PUT` | `/documents/{documentId}/restore` | `SetDocumentRestore` |
| `GET` | `/documents/{documentId}/tags` | `GetDocumentTags` |
| `POST` | `/documents/{documentId}/tags` | `AddDocumentTags` |
| `PATCH` | `/documents/{documentId}/tags` | `UpdateDocumentTags` |
| `PUT` | `/documents/{documentId}/tags` | `SetDocumentTags` |
| `GET` | `/documents/{documentId}/tags/{tagKey}` | `GetDocumentTag` |
| `PUT` | `/documents/{documentId}/tags/{tagKey}` | `SetDocumentTag` |
| `DELETE` | `/documents/{documentId}/tags/{tagKey}` | `DeleteDocumentTag` |
| `DELETE` | `/documents/{documentId}/tags/{tagKey}/{tagValue}` | `DeleteDocumentTagAndValue` |
| `GET` | `/documents/{documentId}/url` | `GetDocumentUrl` |
| `GET` | `/documents/{documentId}/versions` | `GetDocumentVersions` |
| `PUT` | `/documents/{documentId}/versions` | `SetDocumentVersion` |
| `DELETE` | `/documents/{documentId}/versions/{versionKey}` | `DeleteDocumentVersion` |
| `POST` | `/search` | `DocumentSearch` |
| `POST` | `/searchFulltext` | `SearchFulltext` |
| `GET` | `/version` | `GetVersion` |

The curated catalog is defined by `ALLOWED_OPERATION_IDS` in `formkiq_mcp/openapi.py`. Startup rejects specs with missing catalog operations, duplicate operation IDs, or unresolved/external references. Operation-level parameters override path-level parameters. Custom documents must use OpenAPI 3.0 and JSON request bodies.

## Generic Tool Shape

`call_formkiq_operation` accepts nested arguments or unambiguous top-level parameter names. Unknown/conflicting arguments, missing required values, invalid types, and path dot segments are rejected before sending requests. Request bodies follow the schema returned by `describe_operation`; unknown body fields follow that schema’s `additionalProperties` rule.

Nested example:

```json
{
  "operation_id": "GetDocument",
  "args": {
    "path": {
      "documentId": "abc-123"
    },
    "query": {
      "siteId": "default"
    }
  }
}
```

For request bodies:

```json
{
  "operation_id": "DocumentSearch",
  "args": {
    "query": {
      "siteId": "default"
    },
    "body": {
      "query": {
        "text": "invoice"
      }
    }
  }
}
```

## Local Codex Development Setup

For local development from a checkout, use `uv` in `~/.codex/config.toml` or in a trusted project's `.codex/config.toml`:

```toml
[mcp_servers.formkiq]
command = "uv"
args = ["run", "--locked", "python", "server.py"]
cwd = "/Users/you/Documents/github/formkiq-mcp-server"
env_vars = ["FORMKIQ_API_ENDPOINT_URL", "FORMKIQ_JWT"]
startup_timeout_sec = 30
tool_timeout_sec = 60
enabled = true

[mcp_servers.formkiq.env]
MCP_TRANSPORT = "stdio"
```

If you want to pin the spec to a local checkout, add:

```toml
FORMKIQ_OPENAPI_FILE = "/Users/you/Documents/github/formkiq-core/docs/openapi/openapi-jwt.yaml"
```

Then start a new Codex session and check:

```text
/mcp
```

You can also verify from a shell:

```bash
codex mcp list
```

Codex MCP configuration reference: https://developers.openai.com/codex/mcp

## Claude Code Project Setup

For a project-shared Claude Code setup from a local checkout, add a `.mcp.json` file to your project:

```json
{
  "mcpServers": {
    "formkiq": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/Users/you/Documents/github/formkiq-mcp-server", "run", "--locked", "python", "server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "FORMKIQ_API_ENDPOINT_URL": "https://abc123.execute-api.us-east-1.amazonaws.com",
        "FORMKIQ_JWT": "eyJ..."
      }
    }
  }
}
```

Then run:

```bash
claude mcp list
```

Inside Claude Code, use:

```text
/mcp
```

Claude Code MCP docs: https://docs.anthropic.com/en/docs/claude-code/mcp

## Claude Desktop Setup

Claude Desktop uses `claude_desktop_config.json`.

Config file locations:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Example:

```json
{
  "mcpServers": {
    "formkiq": {
      "command": "uv",
      "args": ["--directory", "/Users/you/Documents/github/formkiq-mcp-server", "run", "--locked", "python", "server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "FORMKIQ_API_ENDPOINT_URL": "https://abc123.execute-api.us-east-1.amazonaws.com",
        "FORMKIQ_JWT": "eyJ..."
      }
    }
  }
}
```

Restart Claude Desktop after editing the config. If the MCP server does not appear, check Claude Desktop's MCP logs:

- macOS: `~/Library/Logs/Claude`
- Windows: `%APPDATA%\Claude\logs`

Claude Desktop local MCP setup docs: https://modelcontextprotocol.io/docs/develop/connect-local-servers

## MCP Inspector

Run the HTTP server:

```bash
uv run --locked python server.py
```

Then start the inspector:

```bash
npx -y @modelcontextprotocol/inspector
```

Connect to:

```text
http://localhost:8000/mcp
```

## Write and Purge Access

Read-only access is the default. To enable the separate `call_formkiq_write_operation` tool:

```bash
export FORMKIQ_ENABLE_WRITES=true
```

The read tool continues to reject mutations. Permanent purge is enabled separately and exposes `purge_document`:

```bash
export FORMKIQ_ENABLE_WRITES=true
export FORMKIQ_ENABLE_PURGE=true
```

Restart the server after changing configuration. MCP clients should request user approval for destructive actions; tool annotations identify reads and mutations. Server-side flags and caller policies enforce access independently of client annotations.

Migration: existing callers that used `call_formkiq_operation` for mutations must enable writes and switch to `call_formkiq_write_operation`. Purge callers must enable both flags and use `purge_document`.

## Response Handling and Limits

Responses retain `operationId`, `method`, `statusCode`, `ok`, and `body`. Full request URLs are omitted to avoid exposing share keys. Upstream API failures produce MCP `isError: true`, preserving the HTTP status, bounded response body, and `Retry-After` when present. Invalid JSON, unsupported binary content, transport failures, and timeouts also produce tool errors. Writes are never retried automatically.

`FORMKIQ_MAX_RESPONSE_BYTES` defaults to `65536` (64 KiB), with a supported range of 1 KiB–10 MiB. The proxy stops reading oversized decoded responses, closes the stream, and returns an explicit error. It also limits serialized tool payloads and operation descriptions. Request fewer results, follow pagination tokens, or use `GetDocumentUrl` for large content. The explicitly requested full OpenAPI resource is exempt from this limit; use `describe_operation` for focused schemas.

## HTTP Caller Authentication

Local HTTP binds to `127.0.0.1:8000` by default. `FORMKIQ_MCP_HOST` and `FORMKIQ_MCP_PORT` configure the listener. Non-loopback HTTP refuses to start without `FORMKIQ_MCP_AUTH_FILE`. A reverse proxy that exposes a loopback listener publicly must also use caller authentication.

HTTP authentication uses preconfigured MCP bearer tokens. Configure the token manually in the MCP client; this mode does not provide an OAuth authorization server or automatic OAuth login. MCP tokens are distinct from downstream FormKiQ JWTs and are never forwarded to FormKiQ.

Generate a high-entropy token and its SHA-256 hash:

```bash
python3 - <<'PYTOKEN'
import hashlib
import secrets
token = secrets.token_urlsafe(32)
print("MCP client token:", token)
print("token_sha256:", hashlib.sha256(token.encode()).hexdigest())
PYTOKEN
```

Store only the hash in a private policy file:

```json
{
  "clients": [
    {
      "id": "analyst",
      "token_sha256": "<64-character-lowercase-sha256>",
      "jwt_env": "FORMKIQ_ANALYST_JWT",
      "allowed_operations": ["GetDocuments", "GetDocument", "DocumentSearch"],
      "allowed_sites": ["tenant-a"]
    }
  ]
}
```

```bash
export FORMKIQ_MCP_AUTH_FILE="/private/path/formkiq-callers.json"
export FORMKIQ_ANALYST_JWT="<FormKiQ JWT scoped to this caller>"
export FORMKIQ_MCP_HOST="0.0.0.0"
uv run --locked python server.py
```

Each request requires `Authorization: Bearer <MCP client token>`. Policies restrict operation IDs and site IDs, and each caller maps to its own downstream JWT variable. Missing `siteId` means `default`, which must be explicitly authorized; a tenant-specific caller should supply its site. Global write/purge flags remain required even if a caller policy lists those operations. Policy files load at startup; restart to rotate tokens or policies. Use least-privilege downstream JWTs and replace them when they expire.

HTTP uses stateless MCP requests so caller identity remains attached to each request. A single async HTTP client is reused for the application lifespan. Requests have a 10-second connect timeout and 30-second network-operation timeout.

## Security and Hosting

- Use the CloudFormation `HttpApiUrl` endpoint for JWT authentication. IAM signing and API-key authentication are not implemented.
- FormKiQ endpoints require HTTPS, except loopback HTTP for local development.
- Do not commit JWTs, MCP bearer tokens, or private caller-policy files. Prefer environment variables and secret management.
- Operation audit records go to stderr and include caller ID, operation ID, outcome, status, and duration. Bodies, document IDs, query parameters, and credentials are excluded.
- Place hosted HTTP behind TLS and configure caller authentication, authorization, and infrastructure rate limits. Use a long-running ASGI-compatible host such as ECS or App Runner. Lambda/API Gateway deployments require an appropriate ASGI adapter and timeout configuration.

## Development and Verification

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m unittest discover -v
uv build
```

Tests cover route protection, referenced parameters and overrides, schema validation, read/write/purge boundaries, response limits, MCP errors, concurrency, caller isolation, HTTP transport, and stdio startup. Backend requests are mocked. CI runs the locked environment on Python 3.11 and 3.14, then installs the wheel in an isolated environment and checks the console entry point and bundled resource.

When updating the bundled spec, use an immutable upstream commit, update `formkiq_mcp/data/provenance.json` with the source and SHA-256, and run the suite. Keep the upstream Apache-2.0 license attribution in that file.

Release tags use `vMAJOR.MINOR.PATCH` and match `[project].version` in `pyproject.toml` (for example, package version `0.1.0` uses tag `v0.1.0`). Before publishing a release, update the package version, regenerate `uv.lock`, update the installation examples to the matching tag, and run the verification steps above. Keep published release tags fixed to their original commits.
