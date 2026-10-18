"""Search-node TLS pinning (#467) and dead-connection handling (#456). No
OpenSearch needed."""

import shutil
import ssl
import subprocess
from pathlib import Path

import pytest
from opensearchpy.exceptions import (
    AuthenticationException,
    ConnectionError,
    ConnectionTimeout,
    NotFoundError,
    SSLError,
)

import elden_ring._client as _client
import opensearch_load
from elden_ring.mcp_server import mcp


def _assert_pinned(client):
    conn = client.transport.get_connection()
    assert isinstance(conn, _client.PinnedCAConnection) and conn.use_ssl
    ctx = conn.pool.conn_kw["ssl_context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert [c["serialNumber"] for c in ctx.get_ca_certs()] == [_ca()["serialNumber"]]
    assert ctx.verify_flags & ssl.VERIFY_X509_STRICT  # the CA has keyUsage (#473)
    assert ctx.check_hostname is False
    assert conn.pool.assert_hostname is False  # node SANs lack the EC2 DNS name
    conn.close()  # a recreated pool keeps the override
    conn._create_urllib3_pool()
    assert conn.pool.assert_hostname is False
    return conn


def _ca():
    (ca,) = ssl.create_default_context(cafile=_client.CA_CERTS).get_ca_certs()
    return ca


def test_pinned_ca_is_a_ca_certificate_in_the_package():
    path = Path(_client.CA_CERTS)
    assert path.parent == Path(_client.__file__).parent  # Dockerfile copies the dir
    text = path.read_text()
    assert text.count("BEGIN CERTIFICATE") == 1 and "PRIVATE" not in text
    subject = dict(x[0] for x in _ca()["subject"])
    assert subject["commonName"] == "elden-ring-search-ca"


def test_pinned_ca_passes_strict_x509():
    # #473: strict mode needs keyUsage keyCertSign on the CA, else every handshake
    # fails with "CA cert does not include key usage extension"
    ctx = ssl.create_default_context(cafile=_client.CA_CERTS)
    assert ctx.verify_flags & ssl.VERIFY_X509_STRICT
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl not installed")
    text = subprocess.run(  # noqa: S603 -- fixed argv, the package's own CA file
        [openssl, "x509", "-in", _client.CA_CERTS, "-noout", "-ext", "keyUsage"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "critical" in text and "Certificate Sign" in text and "CRL Sign" in text


@pytest.fixture
def lambda_client(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setattr(_client, "_get_current_endpoint", lambda: "example.invalid")
    monkeypatch.setattr(_client, "_get_password", lambda: "pw")
    _client.reset_cache()
    yield _client.get_client()
    _client.reset_cache()


def test_lambda_client_verifies_and_fails_fast(lambda_client):
    conn = _assert_pinned(lambda_client)
    assert conn.timeout == 20 < 30  # inside the HTTP API cap
    assert lambda_client.transport.max_retries == 0  # a retry doubles a dead host


def test_loader_client_verifies(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "localhost")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "x")
    _assert_pinned(opensearch_load._get_client())


def _prime_caches():
    _client._cache.endpoint = "old-host"
    _client._cache.password = "old-pw"
    _client._cache.client = object()


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("N/A", "refused", None),
        ConnectionTimeout("TIMEOUT", "timed out", None),
        SSLError("N/A", "bad chain", None),
        AuthenticationException(401, "unauthorized", {}),
    ],
)
def test_unreachable_clears_caches_and_returns_error(exc):
    def tool():
        raise exc

    _prime_caches()
    assert _client.unreachable_as_error(tool)() == {"error": _client.UNREACHABLE}
    assert _client._cache.endpoint is None
    assert _client._cache.password is None
    assert _client._cache.client is None


def test_other_errors_still_raise_and_keep_caches():
    def tool():
        raise NotFoundError(404, "index_not_found_exception", {})

    _prime_caches()
    with pytest.raises(NotFoundError):
        _client.unreachable_as_error(tool)()
    assert _client._cache.endpoint == "old-host"
    _client.reset_cache()


def test_every_tool_is_wrapped_without_changing_its_schema(monkeypatch):
    def dead():
        raise ConnectionTimeout("TIMEOUT", "timed out", None)

    monkeypatch.setattr(_client, "get_client", dead)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert tools["list_entity_types"].fn() == {"error": _client.UNREACHABLE}
    assert tools["list_patch_versions"].fn() == {"error": _client.UNREACHABLE}
    # functools.wraps keeps the signature, so parameters and docs are unchanged.
    search = tools["search_entities"]
    assert search.parameters["required"] == ["query"]
    assert search.description and "patch_version" in search.parameters["properties"]
