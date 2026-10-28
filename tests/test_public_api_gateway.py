"""Exercise the rendered Caddy gateway, including direct-IP TLS without SNI."""

import base64
import datetime
import http.client
import http.server
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import bcrypt
import jinja2
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from conftest import ROOT


CADDY = os.environ.get("CADDY_BINARY") or shutil.which("caddy")
pytestmark = pytest.mark.skipif(not CADDY, reason="Caddy binary required for gateway integration")


def render_gateway(password):
    values = yaml.safe_load((ROOT / "roles/public_api/defaults/main.yml").read_text())
    values.update(public_api_address="127.0.0.1", public_api_upstream_port=11434)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    values["lookup"] = lambda *args: password_hash
    return jinja2.Template((ROOT / "roles/public_api/templates/Caddyfile.j2").read_text()).render(**values)


def test_production_config_uses_ip_profile_and_no_public_admin_api(tmp_path):
    path = tmp_path / "Caddyfile"
    path.write_text(render_gateway(secrets.token_urlsafe(32)))
    path.chmod(0o600)
    result = subprocess.run([CADDY, "adapt", "--config", str(path), "--adapter", "caddyfile"], capture_output=True)
    if result.returncode:
        raise AssertionError("Caddy rejected the production gateway configuration")
    config = json.loads(result.stdout)
    assert config["admin"]["disabled"] is True
    policies = config["apps"]["tls"]["automation"]["policies"]
    assert any(issuer.get("profile") == "shortlived" for policy in policies for issuer in policy["issuers"])
    assert any(
        policy.get("default_sni") == "127.0.0.1"
        for server in config["apps"]["http"]["servers"].values()
        for policy in server.get("tls_connection_policies", [])
    )


def test_ip_https_authentication_and_admin_route_denial(tmp_path):
    password = secrets.token_urlsafe(32)
    received = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization"), self.headers.get("Cookie")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"models":[]}')

        def log_message(self, *args):
            pass

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local gateway test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_file, cert_file = tmp_path / "key.pem", tmp_path / "cert.pem"
    key_file.touch(mode=0o600)
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    config = render_gateway(password)
    issuer_start = config.index("    tls {")
    issuer_end = config.index("    header {", issuer_start)
    config = config[:issuer_start] + f"    tls {cert_file} {key_file}\n" + config[issuer_end:]
    config = config.replace("https://127.0.0.1 {", f"https://127.0.0.1:{port} {{")
    config = config.replace("127.0.0.1:11434", f"127.0.0.1:{upstream.server_port}")
    config = config.replace("    admin off", "    admin off\n    auto_https disable_redirects")
    config_file = tmp_path / "Caddyfile"
    config_file.touch(mode=0o600)
    config_file.write_text(config)
    environment = dict(os.environ, XDG_DATA_HOME=str(tmp_path / "data"), XDG_CONFIG_HOME=str(tmp_path / "config"))
    process = subprocess.Popen([CADDY, "run", "--config", str(config_file), "--adapter", "caddyfile"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
    context = ssl.create_default_context(cafile=str(cert_file))

    def request(path, authenticated=False):
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
        headers = {}
        if authenticated:
            headers["Authorization"] = "Basic " + base64.b64encode(f"llm:{password}".encode()).decode()
            headers["Cookie"] = "example=blocked"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    try:
        for _ in range(50):
            try:
                status = request("/api/tags")
                break
            except (OSError, ssl.SSLError):
                time.sleep(0.1)
        else:
            raise AssertionError("Local Caddy IP HTTPS listener did not become ready")
        assert status == 401
        assert request("/api/tags", authenticated=True) == 200
        assert request("/api/delete", authenticated=True) == 404
        assert received == [("/api/tags", None, None)]
    finally:
        process.terminate()
        process.wait(timeout=10)
        upstream.shutdown()
        upstream.server_close()
