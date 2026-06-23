# FormKiQ MCP Server

MCP server for FormKiQ, exposing the FormKiQ OpenAPI spec and selected FormKiQ API operations as Model Context Protocol tools.

This server:

- exposes the FormKiQ JWT OpenAPI spec as an MCP resource at `openapi://formkiq/jwt`
- parses operation metadata from the OpenAPI document
- proxies a curated set of FormKiQ operations as MCP tools
- forwards calls to a deployed FormKiQ API using a JWT

The OpenAPI source defaults to:

```text
https://raw.githubusercontent.com/formkiq/formkiq-core/refs/heads/master/docs/openapi/openapi-jwt.yaml
```

## Requirements

- Python 3.11+
- `uv`
- a deployed FormKiQ API endpoint URL from the FormKiQ CloudFormation outputs
- a FormKiQ JWT for API calls

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Install

```bash
git clone https://github.com/formkiq/formkiq-mcp-server.git
cd formkiq-mcp-server
uv sync
```

You can also run directly from GitHub with `pipx`:

```bash
pipx run --spec git+https://github.com/formkiq/formkiq-mcp-server.git formkiq-mcp-server
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

For IAM or API key endpoint experiments, change the output key to `IamApiUrl` or `KeyApiUrl` and point `FORMKIQ_OPENAPI_URL` or `FORMKIQ_OPENAPI_FILE` at the matching OpenAPI spec.

`FORMKIQ_API_BASE_URL` is still accepted as a deprecated alias for compatibility, but new configs should use `FORMKIQ_API_ENDPOINT_URL`.

Optional: use a local OpenAPI file instead of downloading from GitHub:

```bash
export FORMKIQ_OPENAPI_FILE="/path/to/formkiq-core/docs/openapi/openapi-jwt.yaml"
```

Optional: use a different OpenAPI URL:

```bash
export FORMKIQ_OPENAPI_URL="https://example.com/openapi-jwt.yaml"
```

## Run Locally Over HTTP

```bash
uv run python server.py
```

Or, after installing/running through `pipx`:

```bash
pipx run --spec git+https://github.com/formkiq/formkiq-mcp-server.git formkiq-mcp-server
```

By default, the Python MCP SDK serves Streamable HTTP at:

```text
http://localhost:8000/mcp
```

Use this mode for MCP clients that connect to an already-running HTTP server.

## Run Over Stdio

Most local AI clients start MCP servers as child processes over stdio. This server supports that mode with:

```bash
MCP_TRANSPORT=stdio uv run python server.py
```

You normally do not run that command manually. Instead, put it in your MCP client configuration.

## Tools

This server exposes a curated allowlist:

- `list_formkiq_operations`
- `call_formkiq_operation`
- `get_version`
- `get_documents`
- `get_document`
- `search_documents`

Use `call_formkiq_operation` for the full allowlist below. The convenience tools intentionally cover only common read/search workflows.

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

You can expand `ALLOWED_OPERATION_IDS` in `server.py`. Be careful when exposing destructive or administrative operations until you have user confirmation, audit logging, and authorization checks in place.

## Generic Tool Shape

`call_formkiq_operation` accepts nested arguments:

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

## Codex Setup

Codex supports MCP servers in `config.toml`.

For a GitHub-backed setup similar to common `pipx` MCP servers, add this to `~/.codex/config.toml` or to a trusted project's `.codex/config.toml`:

```toml
[mcp_servers.formkiq]
command = "pipx"
args = [
  "run",
  "--spec",
  "git+https://github.com/formkiq/formkiq-mcp-server.git",
  "formkiq-mcp-server",
]
env = { "MCP_TRANSPORT" = "stdio", "FORMKIQ_API_ENDPOINT_URL" = "https://abc123.execute-api.us-east-1.amazonaws.com", "FORMKIQ_JWT" = "eyJ..." }
startup_timeout_sec = 30
tool_timeout_sec = 60
enabled = true
enabled_tools = [
  "list_formkiq_operations",
  "call_formkiq_operation",
  "get_version",
  "get_documents",
  "get_document",
  "search_documents",
]
```

For local development from a checkout, use `uv` instead:

```toml
[mcp_servers.formkiq]
command = "uv"
args = ["run", "python", "server.py"]
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

## Claude Code Setup

Claude Code can add a local stdio server from the CLI:

```bash
claude mcp add --transport stdio formkiq \
  --env FORMKIQ_API_ENDPOINT_URL=https://abc123.execute-api.us-east-1.amazonaws.com \
  --env FORMKIQ_JWT=eyJ... \
  --env MCP_TRANSPORT=stdio \
  -- uv --directory /Users/you/Documents/github/formkiq-mcp-server run python server.py
```

For a project-shared setup, add a `.mcp.json` file to your project:

```json
{
  "mcpServers": {
    "formkiq": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/Users/you/Documents/github/formkiq-mcp-server", "run", "python", "server.py"],
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
      "args": ["--directory", "/Users/you/Documents/github/formkiq-mcp-server", "run", "python", "server.py"],
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
uv run python server.py
```

Then start the inspector:

```bash
npx -y @modelcontextprotocol/inspector
```

Connect to:

```text
http://localhost:8000/mcp
```

## Security Notes

- The OpenAPI spec declares `http://localhost` as its server URL, so runtime calls use `FORMKIQ_API_ENDPOINT_URL`, which should be the FormKiQ CloudFormation `HttpApiUrl`, `IamApiUrl`, or `KeyApiUrl` output that matches your authentication mode.
- Do not commit real JWTs to config files.
- Prefer environment variables or your client application's secret management.
- Be careful when exposing write, delete, purge, user/group, API key, or system configuration operations as tools.
- For hosted deployments, authenticate the MCP caller and forward or exchange the caller's token instead of using a shared server token.

## AWS Deployment Note

For a hosted serverless deployment, this maps cleanly to:

```text
API Gateway HTTP API or Lambda Function URL
  -> Lambda container running this server
  -> FormKiQ API
```

For long-running remote MCP usage, consider Streamable HTTP behind API Gateway, Lambda Function URLs, App Runner, or ECS Fargate depending on your streaming/session requirements.
