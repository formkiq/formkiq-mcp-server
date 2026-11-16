"""Local configuration and per-caller HTTP bearer policies."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .openapi import ALLOWED_OPERATION_IDS, OpenApiProxyError
from .operations import API_GROUPS


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        raise OpenApiProxyError(
            "FORMKIQ_API_ENDPOINT_URL must be an absolute URL without credentials, query, or fragment"
        )
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback(parsed.hostname)):
        raise OpenApiProxyError("FormKiQ endpoints must use HTTPS (HTTP is allowed only on loopback)")
    return value


def boolean_env(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name, str(default).lower()).strip().lower()
    if value not in {"true", "false"}:
        raise OpenApiProxyError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class CallerPolicy:
    id: str
    token_sha256: str = field(repr=False)
    jwt_env: str
    allowed_operations: frozenset[str]
    allowed_sites: frozenset[str]


caller_context: ContextVar[CallerPolicy | None] = ContextVar("formkiq_caller", default=None)


def load_policies(filename: str) -> tuple[CallerPolicy, ...]:
    try:
        document = json.loads(Path(filename).read_text(encoding="utf-8"))
        clients = document["clients"]
        if not isinstance(clients, list) or not clients:
            raise ValueError
        policies = []
        for client in clients:
            policy = CallerPolicy(
                id=client["id"],
                token_sha256=client["token_sha256"],
                jwt_env=client["jwt_env"],
                allowed_operations=frozenset(client["allowed_operations"]),
                allowed_sites=frozenset(client["allowed_sites"]),
            )
            if (
                not isinstance(policy.id, str)
                or not re.fullmatch(r"[\w.-]{1,80}", policy.id)
                or not re.fullmatch(r"[0-9a-f]{64}", policy.token_sha256)
                or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", policy.jwt_env)
                or not policy.allowed_operations
                or not policy.allowed_operations <= ALLOWED_OPERATION_IDS
                or any(not isinstance(site, str) or not site for site in policy.allowed_sites)
                or not isinstance(client["allowed_operations"], list)
                or not isinstance(client["allowed_sites"], list)
            ):
                raise ValueError
            if not os.getenv(policy.jwt_env, "").strip():
                raise OpenApiProxyError(f"Missing downstream JWT variable for caller {policy.id}: {policy.jwt_env}")
            policies.append(policy)
        if len({p.id for p in policies}) != len(policies) or len({p.token_sha256 for p in policies}) != len(policies):
            raise ValueError
        return tuple(policies)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, OpenApiProxyError):
            raise
        raise OpenApiProxyError(
            "Invalid FORMKIQ_MCP_AUTH_FILE: check IDs, token hashes, JWT variables, operations, and sites"
        ) from exc


@dataclass(frozen=True)
class Settings:
    api_endpoint: str = ""
    jwt: str | None = field(default=None, repr=False)
    enable_writes: bool = False
    enable_purge: bool = False
    max_response_bytes: int = 65_536
    host: str = "127.0.0.1"
    port: int = 8000
    callers: tuple[CallerPolicy, ...] = ()
    api_groups: frozenset[str] = frozenset({"core"})
    modules: frozenset[str] = frozenset()
    auto_discover: bool = True

    def __post_init__(self) -> None:
        if not self.api_groups <= API_GROUPS:
            raise OpenApiProxyError("Unknown FORMKIQ_API_GROUPS; choose from " + ", ".join(sorted(API_GROUPS)))
        object.__setattr__(self, "api_groups", self.api_groups | {"core"})
        if any(not isinstance(module, str) or not module.strip() for module in self.modules):
            raise OpenApiProxyError("FORMKIQ_MODULES must contain nonempty module names")
        if self.api_endpoint:
            object.__setattr__(self, "api_endpoint", endpoint_url(self.api_endpoint))
        if self.enable_purge and not self.enable_writes:
            raise OpenApiProxyError("FORMKIQ_ENABLE_PURGE also requires FORMKIQ_ENABLE_WRITES=true")
        if not 1024 <= self.max_response_bytes <= 10_485_760:
            raise OpenApiProxyError("FORMKIQ_MAX_RESPONSE_BYTES must be between 1024 and 10485760")
        if not 1 <= self.port <= 65535:
            raise OpenApiProxyError("FORMKIQ_MCP_PORT must be between 1 and 65535")

    @classmethod
    def from_env(cls) -> Settings:
        filename = os.getenv("FORMKIQ_MCP_AUTH_FILE", "").strip()
        api_groups = os.getenv("FORMKIQ_API_GROUPS", "core")
        try:
            return cls(
                api_endpoint=os.getenv("FORMKIQ_API_ENDPOINT_URL", "").strip()
                or os.getenv("FORMKIQ_API_BASE_URL", "").strip(),
                jwt=os.getenv("FORMKIQ_JWT", "").strip() or None,
                enable_writes=boolean_env("FORMKIQ_ENABLE_WRITES"),
                enable_purge=boolean_env("FORMKIQ_ENABLE_PURGE"),
                auto_discover=boolean_env("FORMKIQ_AUTO_DISCOVER", default=True),
                max_response_bytes=int(os.getenv("FORMKIQ_MAX_RESPONSE_BYTES", "65536")),
                host=os.getenv("FORMKIQ_MCP_HOST", "127.0.0.1"),
                port=int(os.getenv("FORMKIQ_MCP_PORT", "8000")),
                callers=load_policies(filename) if filename else (),
                api_groups=frozenset(filter(None, (value.strip() for value in api_groups.split(",")))),
                modules=frozenset(
                    filter(None, (value.strip() for value in os.getenv("FORMKIQ_MODULES", "").split(",")))
                ),
            )
        except ValueError as exc:
            if isinstance(exc, OpenApiProxyError):
                raise
            raise OpenApiProxyError("Response limit and port must be integers") from exc


class CallerAuthMiddleware:
    """Authenticate a configured HTTP token; never forward it to FormKiQ."""

    def __init__(self, app: ASGIApp, policies: tuple[CallerPolicy, ...]):
        self.app = app
        self.policies = policies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
        policy = None
        if len(authorization) == 1:
            scheme, _, token = authorization[0].partition(b" ")
            if scheme.lower() == b"bearer" and token and len(token) <= 8192:
                digest = hashlib.sha256(token).hexdigest()
                for candidate in self.policies:
                    if hmac.compare_digest(digest, candidate.token_sha256):
                        policy = candidate
        if policy is None:
            response = JSONResponse(
                {"error": "A valid MCP bearer token is required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="formkiq-mcp"'},
            )
            await response(scope, receive, send)
            return
        context = caller_context.set(policy)
        try:
            await self.app(scope, receive, send)
        finally:
            caller_context.reset(context)
