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

These instructions use MCP server release `v0.2.0`, matching `pyproject.toml`. For local development, use the checkout-based [Install](#install) and [Local Codex Development Setup](#local-codex-development-setup) instructions. Published releases are listed in [Releases](https://github.com/formkiq/formkiq-mcp-server/releases).

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
args = ["run", "--spec", "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.2.0", "formkiq-mcp-server"]
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
  -- pipx run --spec "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.2.0" formkiq-mcp-server
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
- Git for cloning the repository
- `pipx` for GitHub-backed MCP client setup, or `uv` for local development
- a deployed FormKiQ API endpoint URL from the FormKiQ CloudFormation outputs
- a FormKiQ JWT for API calls

### Install uv

**macOS** — with [Homebrew](https://brew.sh/):

```bash
brew install uv
```

**Windows** — in PowerShell, with WinGet:

```powershell
winget install --id=astral-sh.uv -e
```

**Linux** — with the official standalone installer:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal after installation so PATH changes take effect, then verify:

```text
uv --version
```

If `uv` is still not found, follow the installer's PATH instructions. See the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for alternatives, including the Windows standalone installer and the macOS installer without Homebrew.

### Optional: Install pipx

The local checkout workflow below uses `uv`. If you use the release-based MCP client examples, install `pipx` after `uv` on any of these operating systems:

```text
uv tool install pipx
pipx --version
```

See [uv's tool installation guide](https://docs.astral.sh/uv/guides/tools/) if the `pipx` executable needs to be added to PATH.

## Install

Run these commands in Terminal on macOS/Linux or PowerShell on Windows:

```text
git clone https://github.com/formkiq/formkiq-mcp-server.git
cd formkiq-mcp-server
uv sync --locked
```

You can also run release `v0.2.0` directly from GitHub with `pipx`:

```bash
pipx run --spec "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.2.0" formkiq-mcp-server
```

## Configure

Set the FormKiQ API endpoint and a current Cognito `AccessToken` from the same deployment. Set `FORMKIQ_JWT` to the token text; the server adds the `Bearer` prefix. Restart the server after changing the token. For JWT authentication, `FORMKIQ_API_ENDPOINT_URL` should be the `HttpApiUrl` value from the FormKiQ CloudFormation stack outputs.

For local development, copy the provided template without overwriting an existing file.

macOS/Linux:

```bash
cp -n .env.example .env
```

Windows PowerShell:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env` with your deployment values:

```dotenv
FORMKIQ_API_ENDPOINT_URL=https://abc123.execute-api.us-east-1.amazonaws.com
FORMKIQ_JWT=your-raw-Cognito-AccessToken
MCP_TRANSPORT=streamable-http
FORMKIQ_API_GROUPS=core
```

Then start the server normally:

```bash
uv run --locked python server.py
```

Both `python server.py` and the installed `formkiq-mcp-server` entry point automatically load `.env` from the current working directory at startup. The server does not search parent directories. Existing environment variables, including empty values, take precedence over the file. If you previously exported an endpoint or JWT and want to use the file instead, clear those exports first:

```bash
unset FORMKIQ_API_ENDPOINT_URL FORMKIQ_API_BASE_URL FORMKIQ_JWT
```

In Windows PowerShell:

```powershell
Remove-Item Env:FORMKIQ_API_ENDPOINT_URL, Env:FORMKIQ_API_BASE_URL, Env:FORMKIQ_JWT -ErrorAction SilentlyContinue
```

The template includes API Groups, module declarations, transport, and write/purge flags. File values are literal; `${VAR}` interpolation is disabled. `.env` and `.env.*` files are ignored by Git, with `.env.example` included as the shareable template. Restart after editing the file; JWT refresh is still manual.

If your MCP client launches the server from a different directory, configure its working directory or use an explicit file path with `uv`:

```bash
uv run --locked --env-file /absolute/path/to/.env python server.py
```

`uv` reads the explicitly selected file before starting Python. Operators can set `PYTHON_DOTENV_DISABLED=1` to disable the server's automatic `.env` loading. No file is required when configuration is supplied by the host or MCP client.

Alternatively, export settings directly:

```bash
export FORMKIQ_API_ENDPOINT_URL="https://abc123.execute-api.us-east-1.amazonaws.com"
export FORMKIQ_JWT="<raw-Cognito-AccessToken>"
```

Windows PowerShell equivalent:

```powershell
$env:FORMKIQ_API_ENDPOINT_URL = "https://abc123.execute-api.us-east-1.amazonaws.com"
$env:FORMKIQ_JWT = "<raw-Cognito-AccessToken>"
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
pipx run --spec "git+https://github.com/formkiq/formkiq-mcp-server.git@v0.2.0" formkiq-mcp-server
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

Every enabled endpoint has a dedicated MCP tool generated from the pinned OpenAPI spec. Each tool exposes its path/query/header parameters and JSON request body schema, including required fields, types, enums, and referenced definitions. Tool names follow the operation ID in snake case (for example, `GetDocumentOcr` becomes `get_document_ocr`); `DocumentSearch` retains the established name `search_documents`.

The default `core` configuration exposes **29 tools**: 25 endpoint tools plus four supporting tools:

- `list_formkiq_operations` lists enabled endpoints, API groups, permissions, and `toolName`.
- `describe_operation` provides focused schemas and examples for an operation ID.
- `call_formkiq_operation` retains the generic read/search interface.
- `count_documents` adds explicit count truncation/exactness information.

API Groups, installed modules, write/purge flags, and the current caller's operation allowlist filter both endpoint discovery and invocation. With all groups and required modules, read-only access exposes 40 endpoint tools plus the four supporting tools. Enabling writes and purge exposes up to 69 endpoint tools, with an additional generic write tool. The table below maps all supported endpoints to their tools.

Dedicated tools accept API parameter names directly. For example, call `get_document` with:

```json
{"documentId":"abc-123","siteId":"default"}
```

Or call `get_folder_documents` with:

```json
{"path":"invoices/2026","siteId":"default","limit":"10"}
```

JSON request bodies go in `body`, for example `add_document_tags` (with writes enabled):

```json
{"documentId":"abc-123","siteId":"default","body":{"tags":[{"key":"type","value":"invoice"}]}}
```

Parameters keep the API's types (`limit` is usually a string). If a custom spec uses the same parameter name in multiple locations, tools expose location-prefixed names such as `path_id` and `query_id`; an API parameter named `body` also receives a location prefix. Unexpected fields are rejected before any backend request.

### Supported FormKiQ API Endpoints

| Method | Path | Operation ID | Tool | API group | Access |
|---|---|---|---|---|---|
| `GET` | `/activities` | `GetResourceActivities` | `get_resource_activities` | `audit` | `read` |
| `GET` | `/attributes` | `GetAttributes` | `get_attributes` | `core` | `read` |
| `POST` | `/attributes` | `AddAttribute` | `add_attribute` | `core` | `write` |
| `DELETE` | `/attributes/{key}` | `DeleteAttribute` | `delete_attribute` | `core` | `write` |
| `GET` | `/attributes/{key}` | `GetAttribute` | `get_attribute` | `core` | `read` |
| `PATCH` | `/attributes/{key}` | `UpdateAttribute` | `update_attribute` | `core` | `write` |
| `GET` | `/attributes/{key}/allowedValues` | `GetAttributeAllowedValues` | `get_attribute_allowed_values` | `core` | `read` |
| `GET` | `/documents` | `GetDocuments` | `get_documents` | `core` | `read` |
| `POST` | `/documents` | `AddDocument` | `add_document` | `core` | `write` |
| `POST` | `/documents/upload` | `AddDocumentUpload` | `add_document_upload` | `core` | `write` |
| `DELETE` | `/documents/{documentId}` | `DeleteDocument` | `delete_document` | `core` | `write` |
| `GET` | `/documents/{documentId}` | `GetDocument` | `get_document` | `core` | `read` |
| `PATCH` | `/documents/{documentId}` | `UpdateDocument` | `update_document` | `core` | `write` |
| `GET` | `/documents/{documentId}/actions` | `GetDocumentActions` | `get_document_actions` | `core` | `read` |
| `POST` | `/documents/{documentId}/actions` | `AddDocumentActions` | `add_document_actions` | `core` | `write` |
| `POST` | `/documents/{documentId}/actions/retry` | `AddDocumentRetryAction` | `add_document_retry_action` | `core` | `write` |
| `GET` | `/documents/{documentId}/ai/prompts` | `GetDocumentAiPromptsResults` | `get_document_ai_prompts_results` | `ai` | `read` |
| `GET` | `/documents/{documentId}/ai/prompts/{llmPromptEntityName}` | `GetDocumentAiPromptResults` | `get_document_ai_prompt_results` | `ai` | `read` |
| `GET` | `/documents/{documentId}/artifacts` | `GetDocumentArtifacts` | `get_document_artifacts` | `core` | `read` |
| `GET` | `/documents/{documentId}/attributes` | `GetDocumentAttributes` | `get_document_attributes` | `core` | `read` |
| `POST` | `/documents/{documentId}/attributes` | `AddDocumentAttributes` | `add_document_attributes` | `core` | `write` |
| `PUT` | `/documents/{documentId}/attributes` | `SetDocumentAttributes` | `set_document_attributes` | `core` | `write` |
| `DELETE` | `/documents/{documentId}/attributes/{attributeKey}` | `DeleteDocumentAttribute` | `delete_document_attribute` | `core` | `write` |
| `GET` | `/documents/{documentId}/attributes/{attributeKey}` | `GetDocumentAttribute` | `get_document_attribute` | `core` | `read` |
| `PUT` | `/documents/{documentId}/attributes/{attributeKey}` | `SetDocumentAttributeValue` | `set_document_attribute_value` | `core` | `write` |
| `DELETE` | `/documents/{documentId}/attributes/{attributeKey}/{attributeValue}` | `DeleteDocumentAttributeAndValue` | `delete_document_attribute_and_value` | `core` | `write` |
| `GET` | `/documents/{documentId}/content` | `GetDocumentContent` | `get_document_content` | `core` | `read` |
| `GET` | `/documents/{documentId}/fulltext` | `GetDocumentFulltext` | `get_document_fulltext` | `fulltext` | `read` |
| `GET` | `/documents/{documentId}/malwareScan` | `GetMalwareScanResults` | `get_malware_scan_results` | `malware` | `read` |
| `GET` | `/documents/{documentId}/ocr` | `GetDocumentOcr` | `get_document_ocr` | `ocr` | `read` |
| `POST` | `/documents/{documentId}/ocr` | `AddDocumentOcr` | `add_document_ocr` | `ocr` | `write` |
| `DELETE` | `/documents/{documentId}/purge` | `PurgeDocument` | `purge_document` | `core` | `purge` |
| `PUT` | `/documents/{documentId}/restore` | `SetDocumentRestore` | `set_document_restore` | `core` | `write` |
| `GET` | `/documents/{documentId}/tags` | `GetDocumentTags` | `get_document_tags` | `core` | `read` |
| `PATCH` | `/documents/{documentId}/tags` | `UpdateDocumentTags` | `update_document_tags` | `core` | `write` |
| `POST` | `/documents/{documentId}/tags` | `AddDocumentTags` | `add_document_tags` | `core` | `write` |
| `PUT` | `/documents/{documentId}/tags` | `SetDocumentTags` | `set_document_tags` | `core` | `write` |
| `DELETE` | `/documents/{documentId}/tags/{tagKey}` | `DeleteDocumentTag` | `delete_document_tag` | `core` | `write` |
| `GET` | `/documents/{documentId}/tags/{tagKey}` | `GetDocumentTag` | `get_document_tag` | `core` | `read` |
| `PUT` | `/documents/{documentId}/tags/{tagKey}` | `SetDocumentTag` | `set_document_tag` | `core` | `write` |
| `DELETE` | `/documents/{documentId}/tags/{tagKey}/{tagValue}` | `DeleteDocumentTagAndValue` | `delete_document_tag_and_value` | `core` | `write` |
| `GET` | `/documents/{documentId}/upload` | `GetDocumentIdUpload` | `get_document_id_upload` | `core` | `write` |
| `GET` | `/documents/{documentId}/url` | `GetDocumentUrl` | `get_document_url` | `core` | `read` |
| `GET` | `/documents/{documentId}/versions` | `GetDocumentVersions` | `get_document_versions` | `core` | `read` |
| `PUT` | `/documents/{documentId}/versions` | `SetDocumentVersion` | `set_document_version` | `core` | `write` |
| `DELETE` | `/documents/{documentId}/versions/{versionKey}` | `DeleteDocumentVersion` | `delete_document_version` | `core` | `write` |
| `POST` | `/documents/{documentId}/workflow/{workflowId}/decisions` | `AddDocumentWorkflowDecisions` | `add_document_workflow_decisions` | `workflows` | `write` |
| `GET` | `/documents/{documentId}/workflows` | `GetDocumentWorkflows` | `get_document_workflows` | `workflows` | `read` |
| `POST` | `/documents/{documentId}/workflows` | `AddDocumentWorkflow` | `add_document_workflow` | `workflows` | `write` |
| `GET` | `/documents/{documentId}/workflows/{workflowId}` | `GetDocumentWorkflow` | `get_document_workflow` | `workflows` | `read` |
| `GET` | `/folders` | `GetFolderDocuments` | `get_folder_documents` | `core` | `read` |
| `POST` | `/folders` | `AddFolder` | `add_folder` | `core` | `write` |
| `POST` | `/indices/search` | `IndexSearch` | `index_search` | `core` | `read` |
| `GET` | `/permissions/catalog` | `GetPermissionCatalog` | `get_permission_catalog` | `core` | `read` |
| `GET` | `/queues` | `GetQueues` | `get_queues` | `workflows` | `read` |
| `GET` | `/queues/{queueId}` | `GetQueue` | `get_queue` | `workflows` | `read` |
| `GET` | `/queues/{queueId}/documents` | `GetWorkflowQueueDocuments` | `get_workflow_queue_documents` | `workflows` | `read` |
| `POST` | `/search` | `DocumentSearch` | `search_documents` | `core` | `read` |
| `POST` | `/searchFulltext` | `SearchFulltext` | `search_fulltext` | `fulltext` | `read` |
| `GET` | `/sites` | `GetSites` | `get_sites` | `core` | `read` |
| `GET` | `/sites/{siteId}/classifications` | `GetSitesClassifications` | `get_sites_classifications` | `core` | `read` |
| `GET` | `/sites/{siteId}/classifications/{classificationId}` | `GetClassification` | `get_classification` | `core` | `read` |
| `GET` | `/sites/{siteId}/classifications/{classificationId}/attributes/{key}/allowedValues` | `GetClassificationAttributeAllowedValues` | `get_classification_attribute_allowed_values` | `core` | `read` |
| `GET` | `/sites/{siteId}/schema/document` | `GetSitesSchema` | `get_sites_schema` | `core` | `read` |
| `GET` | `/sites/{siteId}/schema/document/attributes/{key}/allowedValues` | `GetSitesSchemaAttributeAllowedValues` | `get_sites_schema_attribute_allowed_values` | `core` | `read` |
| `GET` | `/version` | `GetVersion` | `get_version` | `core` | `read` |
| `GET` | `/workflows` | `GetWorkflows` | `get_workflows` | `workflows` | `read` |
| `GET` | `/workflows/{workflowId}` | `GetWorkflow` | `get_workflow` | `workflows` | `read` |
| `GET` | `/workflows/{workflowId}/documents` | `GetWorkflowDocuments` | `get_workflow_documents` | `workflows` | `read` |

The explicit operation registry is in `formkiq_mcp/operations.py`; access is assigned by effect, not HTTP verb or name. `GetDocumentIdUpload` is a write because it grants upload capability, while `IndexSearch` is a read despite using POST. `requiredPermission` is preserved from the upstream spec for guidance; FormKiQ remains the authority for permissions.

Startup rejects specs with missing catalog operations, duplicate operation IDs, or unresolved/external references. Custom documents must include all 69 supported operations, use OpenAPI 3.0, and use JSON request bodies.
## API Groups and Installed Modules

The default API group is `core`, which is always included. Additional API groups are opt-in:

| API group | Operations | Deployment capability |
|---|---|---|
| `core` | Document and attribute operations, folders/index search, site discovery, site schemas/classifications | Common API endpoints; backend permissions still apply |
| `ocr` | `GetDocumentOcr`, `AddDocumentOcr` | Tesseract is available without an add-on; Textract-specific options require `textract` |
| `fulltext` | `SearchFulltext`, `GetDocumentFulltext` | `opensearch` |
| `ai` | `GetDocumentAiPromptsResults`, `GetDocumentAiPromptResults` | `ai` (reported `dataclassification` is recognized) |
| `malware` | `GetMalwareScanResults` | `malware` (reported `antivirus`/`malwarescan` are recognized) |
| `workflows` | Workflow/queue inspection, starting document workflows, submitting decisions | `workflows` (reported `workflow` is recognized) |
| `audit` | `GetResourceActivities` | Explicit API group opt-in; backend `GOVERN_USER_ACTIVITIES` permission |

```bash
export FORMKIQ_API_GROUPS="core,ocr,fulltext,ai,malware,workflows"
```

Leaving `FORMKIQ_API_GROUPS` unset or empty selects only `core`.

Installed modules are **discovered automatically at startup**. When an endpoint and JWT are configured, the server makes one read-only `GetVersion` request, with a five-second overall timeout, before serving the first tool list. You do not need to call `get_version` manually to expose module-backed tools. Only selected API Groups are exposed, and write/purge flags and caller grants still apply.

For hosted HTTP, startup discovery uses the first configured caller whose operation allowlist includes `GetVersion`, with that caller's downstream JWT. If no caller grants `GetVersion`, discovery is skipped; it does not fall back to the global JWT to bypass caller policies. Allow `GetVersion` for a discovery caller, or declare installed capabilities with `FORMKIQ_MODULES`.

Discovery failures do not prevent startup. Missing credentials, authorization failures, unreachable endpoints, and invalid module responses are reported on stderr. Selected groups with missing capabilities are also named, with a reminder to declare installed but unadvertised modules. Explicit `FORMKIQ_MODULES` declarations remain effective even if discovery fails. Unknown module names are ignored.

For stdio clients, pass optional settings through the client's environment table or allowlist, or use `.env`, then restart the child server. To disable startup discovery for offline use or fully explicit configuration:

```dotenv
FORMKIQ_AUTO_DISCOVER=false
```

You can still call `get_version` to retry failed discovery or refresh capabilities after the backend changes. Then refresh the client's tool list (MCP `tools/list`); clients that cache tools may need a manual refresh, as the server does not send tool-list change notifications. Automatic discovery runs once per server lifespan, rather than on every request.

Some deployments do not advertise every feature in `GetVersion.modules`. Declare those installed capabilities explicitly:

```bash
export FORMKIQ_MODULES="workflows"
```

Supported capability names include `opensearch`, `typesense`, `textract`, `ai`, `malware`, and `workflows`. Versioned identifiers such as `opensearch_2.19.2` are recognized. Explicit declarations supplement discovered modules; they do not install features or grant permissions. Read-only configuration remains the default even when all optional API groups are selected. `DocumentSearch` remains in `core`; its structured and JSON predicates do not require OpenSearch.

Administration, integrations, deprecated processing APIs, and public/provider callback endpoints are outside this version's catalog.

## Search, Counts, and Explicit Deletion

`search_documents` accepts `body`, `siteId`, `next`, `previous`, `limit` (a string, as defined by the API), and `projection` (`DOCUMENTS` or `COUNT`). Use `describe_operation("DocumentSearch")` for multi-attribute and nested JSON examples. Continue document pagination while `next` is present, even if a filtered page contains no matches.

Example `count_documents` arguments:

```json
{"body":{"query":{"attribute":{"key":"invoiceDetails","json":{"path":"$.total","gte":1000}}}},"siteId":"default"}
```

The count helper uses the existing `DocumentSearch` endpoint with `projection=COUNT`. It preserves `body.count` and `body.truncated` and adds `countIsExact`. A truncated count is a lower bound, capped at 10,000 or stopped by the backend processing budget. COUNT has no continuation tokens, and the helper never aggregates partial counts. Invalid or older backend responses are reported as errors rather than treated as exact totals.

When writes are enabled, `delete_document` requires an explicit `deleteType`: `SOFT_DELETE`, `HARD_DELETE`, or `CONTENT_ONLY`. The generic write tool also requires `deleteType` for `DeleteDocument`, or an explicit legacy `softDelete` boolean. `CONTENT_ONLY` preserves metadata and historical S3 versions; permanent historical removal still uses the separately enabled purge tool.

The dedicated `get_folder_documents` tool accepts a folder path directly as `path`. For generic calls, folder paths use `args.query.path`; `args.path` is reserved for URL path parameters. Upload/download acceleration remains available as the `accelerate` parameter on URL operations.

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
env_vars = ["FORMKIQ_API_ENDPOINT_URL", "FORMKIQ_JWT", "FORMKIQ_API_GROUPS", "FORMKIQ_MODULES", "FORMKIQ_AUTO_DISCOVER", "FORMKIQ_ENABLE_WRITES", "FORMKIQ_ENABLE_PURGE"]
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

Read-only access is the default. To expose dedicated write tools such as `add_document`, `add_document_tags`, and `delete_document`, along with `call_formkiq_write_operation`:

```bash
export FORMKIQ_ENABLE_WRITES=true
```

Read tools continue to reject mutations. Permanent purge is enabled separately and exposes `purge_document`:

```bash
export FORMKIQ_ENABLE_WRITES=true
export FORMKIQ_ENABLE_PURGE=true
```

Restart the server after changing configuration. MCP clients should request user approval for destructive actions; tool annotations identify reads and mutations. Server-side flags and caller policies enforce access independently of client annotations.

Migration from v0.1.0: endpoints now have dedicated tools with full OpenAPI input schemas. Existing `get_version`, `get_documents`, `get_document`, and `search_documents` names remain available; generic tools also remain supported. `SearchFulltext` requires the `fulltext` API group and an OpenSearch capability; `GetResourceActivities` requires the `audit` API group. Document deletion requires an explicit mode. Custom OpenAPI files must cover 69 operations. Mutations use their dedicated tools or the generic write tool, and purge continues to require both flags and `purge_document`. HTTP tool listings are filtered to each caller's grants.

## Response Handling and Limits

Responses retain `operationId`, `method`, `statusCode`, `ok`, and `body`. Full request URLs are omitted to avoid exposing share keys. Upstream API failures produce MCP `isError: true`, preserving the HTTP status, bounded response body, and `Retry-After` when present. Invalid JSON, unsupported binary content, transport failures, and timeouts also produce tool errors. A read operation with a declared download redirect returns a validated `body.contentUrl` without following the redirect or forwarding the FormKiQ JWT. Accepted processing responses preserve the backend status and body; they are not polled automatically. Issuing an upload URL does not complete the upload, and an accepted OCR/job request does not imply completion. Writes are never retried automatically.

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
      "allowed_operations": ["GetVersion", "GetSites", "GetDocuments", "GetDocument", "DocumentSearch"],
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

Each request requires `Authorization: Bearer <MCP client token>`. Policies restrict operation IDs and site IDs in both path and query parameters, and each caller maps to its own downstream JWT variable. For HTTP callers, `GetSites` returns only allowed site summaries (`siteId`, `title`, `status`, `roles`, `permissions`), excluding global claims and site configuration. Explicit `indexName` overrides are disabled for HTTP callers to preserve site isolation. Missing `siteId` means `default`, which must be explicitly authorized; a tenant-specific caller should supply its site. Global write/purge flags remain required even if a caller policy lists those operations. Policy files load at startup; restart to rotate tokens or policies. Use least-privilege downstream JWTs and replace them when they expire.

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

Tests validate all 69 generated endpoint schemas and route all 29 added endpoints through their dedicated tools. They cover `.env` startup and environment precedence, automatic module discovery before the initial tool list, discovery failures/timeouts/cancellation, explicit operation effects, caller-specific tool discovery and cached-tool authorization, API group/module gating and revocation, path/query/header/body routing, recursive schemas, site isolation, filtered site discovery, nested JSON search, count truncation, deletion modes, download redirects, async acceptance, response limits, concurrency, HTTP transport, and stdio startup. Backend requests are mocked. CI runs the locked environment on Python 3.11 and 3.14, then installs the wheel in an isolated environment and checks the console entry point, generated tool schemas, and bundled resource.

When updating the bundled spec, use an immutable upstream commit, update `formkiq_mcp/data/provenance.json` with the source and SHA-256, and run the suite. Keep the upstream Apache-2.0 license attribution in that file.

Release tags use `vMAJOR.MINOR.PATCH` and match `[project].version` in `pyproject.toml` (for example, package version `0.1.0` uses tag `v0.1.0`). Before publishing a release, update the package version, regenerate `uv.lock`, update the installation examples to the matching tag, and run the verification steps above. Keep published release tags fixed to their original commits.
