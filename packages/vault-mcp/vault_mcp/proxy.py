"""Lambda front end: the always-on MCP endpoint for the on-demand vault box.

claude.ai talks to this Lambda (API Gateway, a stable URL). It lists the same
tools as the box, wrapping the box's own tool functions so the signatures can't
drift, and forwards each call to ``vault-mcp`` on the instance. When the box is
asleep, a call starts it and tells Claude to wait: ``start_vault`` polls until
the box reports Obsidian Sync has caught up. The box stops itself when idle
(deploy/server/bin/vault-idle-stop), so there is no stop here.

The same pattern as soulsearch: no VPC; the instance's public DNS name is looked
up per container (it changes on every start); TLS with the box's own CA, read
from SSM with the bearer token, checking the chain but not the hostname.
"""

import functools
import os
import ssl
import time
from typing import Any

import boto3
import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp_lambda import create_mcp_app

from vault_mcp import mcp_server
from vault_mcp.app import TOKEN_ENV

PARAM_PREFIX = "/obsidian-vault/"
PORT = int(os.environ.get("VAULT_BOX_PORT", "8443"))
# API Gateway cuts HTTP APIs off at 30 s; the function timeout is 29 s.
START_BUDGET = 20.0
CALL_TIMEOUT = 25.0

STARTING = (
    "The vault server was asleep and is starting now. That takes 1-3 minutes, "
    "including Obsidian Sync catching up. Call start_vault() about every 30 seconds "
    "until it reports ready, then retry this request."
)
INSTRUCTIONS = (
    mcp_server.INSTRUCTIONS
    + " The vault server sleeps when idle: if a tool says it is starting or "
    "syncing, call start_vault() until it reports ready, then carry on."
)

_cache: dict[str, Any] = {}


def _instance_id() -> str:
    return os.environ["VAULT_INSTANCE_ID"]


@functools.cache
def _ec2() -> Any:
    return boto3.client("ec2")


def _params() -> dict[str, str]:
    """mcp-token, ca-cert and readonly from SSM, cached per container."""
    if "params" not in _cache:
        names = [PARAM_PREFIX + n for n in ("mcp-token", "ca-cert", "readonly")]
        resp = boto3.client("ssm").get_parameters(Names=names, WithDecryption=True)
        _cache["params"] = {
            p["Name"].removeprefix(PARAM_PREFIX): p["Value"] for p in resp["Parameters"]
        }
    return _cache["params"]


def _forget(*keys: str) -> None:
    for k in keys:
        _cache.pop(k, None)


def _instance() -> dict[str, str | None]:
    resp = _ec2().describe_instances(InstanceIds=[_instance_id()])
    inst = resp["Reservations"][0]["Instances"][0]
    return {
        "state": inst["State"]["Name"],
        "host": inst.get("PublicDnsName") or inst.get("PublicIpAddress") or None,
    }


def _client() -> httpx.Client:
    if "client" not in _cache:
        params = _params()
        if not params.get("ca-cert"):
            _forget("params")
            raise ToolError(
                "The vault server hasn't been provisioned yet (no CA certificate in "
                "SSM); run deploy.sh push."
            )
        ctx = ssl.create_default_context(cadata=params["ca-cert"])
        # The public DNS name changes on every start, so it can't be in the cert.
        ctx.check_hostname = False
        _cache["client"] = httpx.Client(
            verify=ctx,
            timeout=httpx.Timeout(CALL_TIMEOUT, connect=5.0),
            headers={"Authorization": f"Bearer {params['mcp-token']}"},
        )
    return _cache["client"]


def _start() -> None:
    _ec2().start_instances(InstanceIds=[_instance_id()])


def _host_or_start() -> str:
    """The running box's hostname; otherwise start it and raise the 'starting' error."""
    if host := _cache.get("host"):
        return host
    inst = _instance()
    if inst["state"] == "running" and inst["host"]:
        _cache["host"] = inst["host"]
        return inst["host"]
    if inst["state"] == "stopped":
        _start()
        raise ToolError(STARTING)
    if inst["state"] == "pending":
        raise ToolError(STARTING)
    raise ToolError(
        f"The vault server is {inst['state']} (it stops itself when idle). "
        "Call start_vault() in about 30 seconds."
    )


def _get(host: str, path: str) -> httpx.Response:
    return _request(lambda c: c.get(f"https://{host}:{PORT}{path}"))


def _request(send: Any) -> httpx.Response:
    try:
        resp = send(_client())
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Stale hostname (a warm container across a stop/start), still booting,
        # or a TLS failure after the box's CA was rebuilt: re-read everything.
        _forget("host", "client", "params")
        raise ToolError(
            "The vault server isn't answering yet (it may still be booting). "
            "Call start_vault() in about 30 seconds."
        ) from exc
    except httpx.TimeoutException as exc:
        raise ToolError("The vault server took too long to answer; try again.") from exc
    if resp.status_code == 401:
        _forget("client", "params")
        raise ToolError("The vault server rejected the token (rotated?); try again.")
    return resp


def _call(name: str, arguments: dict[str, Any]) -> Any:
    host = _host_or_start()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    resp = _request(
        lambda c: c.post(
            f"https://{host}:{PORT}/mcp/",
            json=body,
            headers={"accept": "application/json, text/event-stream"},
        )
    )
    if resp.status_code != 200:
        raise ToolError(f"vault server error {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if "error" in payload:
        raise ToolError(str(payload["error"].get("message", payload["error"])))
    result = payload["result"]
    text = " ".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise ToolError(text or f"{name} failed on the vault server")
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return text


def _forward(fn: Any) -> Any:
    """A tool with ``fn``'s name, signature and docstring that runs it on the box."""

    @functools.wraps(fn)
    def tool(**kwargs: Any) -> Any:
        return _call(fn.__name__, kwargs)

    return tool


def start_vault() -> dict[str, Any]:
    """Wake the vault server and wait (up to ~20 s per call) until it's ready.

    The server sleeps after 30 idle minutes. Starting takes 1-3 minutes: boot,
    then Obsidian Sync pulling edits made on other devices. Call again every
    ~30 s while it reports "starting".
    """
    deadline = time.monotonic() + START_BUDGET
    while True:
        inst = _instance()
        state, host = inst["state"], inst["host"]
        if state == "stopped":
            _start()
            detail = "starting the instance"
        elif state == "running" and host:
            _cache["host"] = host
            try:
                resp = _get(host, "/readyz")
                ready = resp.json() if resp.status_code == 200 else None
            except ToolError:
                ready = None
            if ready and (ready.get("latched") or ready.get("ready")):
                return {"status": "ready", "sync": ready}
            detail = (
                "instance running; vault server not answering yet"
                if ready is None
                else f"Obsidian Sync catching up (status {ready.get('sync_status')!r}, "
                f"vault quiet for {ready.get('quiet_seconds')} s)"
            )
        else:
            detail = f"instance {state}"
        if time.monotonic() + 3 > deadline:
            return {
                "status": "starting",
                "detail": detail,
                "next": "call start_vault() again in about 30 seconds",
            }
        time.sleep(3)


def health() -> dict[str, Any]:
    """Instance state and, when it's running, the vault server's own health.

    Doesn't wake a sleeping server.
    """
    inst = _instance()
    out: dict[str, Any] = {"instance": inst["state"]}
    if inst["state"] == "running" and inst["host"]:
        try:
            resp = _get(inst["host"], "/healthz")
            out["server"] = resp.json() if resp.status_code == 200 else resp.text[:300]
        except ToolError as exc:
            out["server"] = str(exc)
    return out


def create_proxy_server(readonly: bool) -> MCPServer:
    server = MCPServer("obsidian-vault", instructions=INSTRUCTIONS)
    for fn in mcp_server.READ_TOOLS:
        if fn is not mcp_server.health:
            server.add_tool(_forward(fn), annotations=mcp_server._RO)
    server.add_tool(
        _forward(mcp_server.daily_note),
        annotations=mcp_server._RO if readonly else mcp_server._WRITE,
    )
    if not readonly:
        for fn, ann in mcp_server.WRITE_TOOLS:
            server.add_tool(_forward(fn), annotations=ann)
    server.add_tool(
        start_vault,
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )
    server.add_tool(health, annotations=mcp_server._RO)
    return server


def create_proxy_app() -> FastAPI:
    """The Lambda's app. Same token as the box; fails closed without one."""
    params = _params()
    token = params.get("mcp-token")
    if not token:
        raise RuntimeError(f"SSM {PARAM_PREFIX}mcp-token is not set")
    os.environ[TOKEN_ENV] = token
    readonly = params.get("readonly", "0").strip().lower() in {"1", "true", "yes", "on"}
    return create_mcp_app(
        create_proxy_server(readonly),
        title="Obsidian Vault MCP",
        description="Always-on front end for the on-demand Obsidian vault server.",
        auth_env=TOKEN_ENV,
    )
