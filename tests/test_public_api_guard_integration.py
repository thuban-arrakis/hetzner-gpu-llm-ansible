"""Real Caddy + Go guard boundary; only the application upstream is a stub."""

import base64
import concurrent.futures
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
GO = shutil.which("go")
pytestmark = pytest.mark.skipif(not CADDY or not GO, reason="Caddy and Go required for real guard integration")


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def local_certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local guard integration")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256())
    )
    key_file, cert_file = directory / "key.pem", directory / "cert.pem"
    key_file.touch(mode=0o600)
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_real_gateway_guard_authentication_limits_and_normalized_proxy(tmp_path):
    """Forward-auth clones keep credentials/body, but inference forwarding never does."""
    tmp_path.chmod(0o700)
    environment = dict(os.environ)
    for variable, directory in {
        "GOCACHE": "/private/tmp/llm-guard-gocache" if os.path.isdir("/private/tmp") else str(tmp_path / "go-cache"),
        "GOMODCACHE": str(tmp_path / "go-mod-cache"),
        "GOTMPDIR": str(tmp_path / "go-tmp"),
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }.items():
        os.makedirs(directory, mode=0o700, exist_ok=True)
        environment[variable] = directory
    environment.update(GOTELEMETRY="off", GOTOOLCHAIN="local", GOPROXY="off")
    binary = tmp_path / "llm-api-guard"
    build = subprocess.run([GO, "build", "-o", str(binary), "."], cwd=ROOT / "roles/public_api/files/api_guard",
                           env=environment, capture_output=True, timeout=90)
    assert build.returncode == 0, "Go guard build failed"

    received = []
    application_get_paths = []
    block_next = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    class Application(http.server.BaseHTTPRequestHandler):
        def respond(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            # The same inert application fixture stands in for catalogue/UI assets.
            application_get_paths.append(self.path)
            self.respond(b'{"ready":true}')

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            # Store only a leak flag, never credential/header values in assertion data.
            leaked = any(name.lower() in {"authorization", "cookie"} or name.lower().startswith("x-guard-")
                         for name in self.headers)
            received.append((self.path, json.loads(body), leaked))
            if block_next.is_set():
                entered.set()
                release.wait(timeout=5)
            self.respond(b'{"done":true}')

        def log_message(self, *args):
            pass

    application = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Application)
    threading.Thread(target=application.serve_forever, daemon=True).start()
    guard_port, gateway_port, metrics_port = free_port(), free_port(), free_port()
    password = secrets.token_urlsafe(32)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    values = yaml.safe_load((ROOT / "roles/public_api/defaults/main.yml").read_text())
    values.update(
        public_api_address="127.0.0.1", public_api_upstream_port=application.server_port,
        public_api_guard_port=guard_port, public_api_context_tokens=2048,
        public_api_generation_timeout=10, public_api_inference_rpm=1000, public_api_inference_burst=100,
        public_api_unknown_per_ip_rpm=0.01, public_api_unknown_per_ip_burst=4,
        public_api_unknown_global_rpm=1000, public_api_unknown_global_burst=100,
        monitoring_enabled=True, ollama_model="qwen3:4b-q4_K_M", lookup=lambda *args: password_hash,
    )
    templates = jinja2.Environment(undefined=jinja2.StrictUndefined)
    templates.filters["to_json"] = json.dumps
    guard_config = templates.from_string((ROOT / "roles/public_api/templates/guard-config.json.j2").read_text()).render(**values)
    # Ansible's integer filter rounds RPM, so set the deliberately slow test refill afterwards.
    guard_values = json.loads(guard_config)
    guard_values["unknown_per_ip_rpm"] = 0.01
    guard_file = tmp_path / "guard.json"
    guard_file.write_text(json.dumps(guard_values))
    config = templates.from_string((ROOT / "roles/public_api/templates/Caddyfile.j2").read_text()).render(**values)
    cert_file, key_file = local_certificate(tmp_path)
    issuer_start, issuer_end = config.index("    tls {"), config.index("    header {")
    config = config[:issuer_start] + f"    tls {cert_file} {key_file}\n" + config[issuer_end:]
    config = config.replace("https://127.0.0.1 {", f"https://127.0.0.1:{gateway_port} {{")
    config = config.replace("servers :443", f"servers :{gateway_port}")
    config = config.replace("127.0.0.1:3000", f"127.0.0.1:{application.server_port}")
    config = config.replace("127.0.0.1:2019", f"127.0.0.1:{metrics_port}")
    config = config.replace("    admin off", "    admin off\n    auto_https disable_redirects")
    caddy_file = tmp_path / "Caddyfile"
    caddy_file.touch(mode=0o600)
    caddy_file.write_text(config)
    context = ssl.create_default_context(cafile=str(cert_file))

    def request(path, body=None, authenticated=True, forged_ip=None):
        connection = http.client.HTTPSConnection("127.0.0.1", gateway_port, context=context, timeout=7)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Basic " + base64.b64encode(f"llm:{password}".encode()).decode()
            headers["Cookie"] = "inert=must-not-reach-inference"
        if forged_ip:
            headers.update({"X-Guard-Client-IP": forged_ip, "X-Forwarded-For": forged_ip,
                            "X-Guard-User": "untrusted-client"})
        try:
            connection.request("POST" if body is not None else "GET", path, body=body, headers=headers)
            response = connection.getresponse()
            status, retry = response.status, response.getheader("Retry-After")
            response.read()
            return status, retry
        finally:
            connection.close()

    guard = gateway = None
    try:
        guard = subprocess.Popen([str(binary), "-config", str(guard_file)], env=environment,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        gateway = subprocess.Popen([CADDY, "run", "--config", str(caddy_file), "--adapter", "caddyfile"],
                                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 8
        while True:
            try:
                # Readiness must not spend the intentionally tiny admission budget.
                with socket.create_connection(("127.0.0.1", guard_port), timeout=0.2):
                    pass
                connection = http.client.HTTPSConnection("127.0.0.1", gateway_port, context=context, timeout=0.2)
                connection.connect()
                connection.close()
                break
            except (OSError, ssl.SSLError):
                assert time.monotonic() < deadline, "Local gateway/guard did not become ready"
                time.sleep(0.05)

        # Both real forward_auth hooks need the original Authorization header.
        # A browser asset burst larger than the unknown budget proves the second
        # hook registered the credential digest and subsequent requests hit it.
        assert request("/api/tags")[0] == 200
        assert request("/grafana/api/search?type=dash-db")[0] == 200
        assert application_get_paths[-1] == "/grafana/api/search?type=dash-db"
        for _ in range(6):
            assert request("/grafana/public/build/runtime.js")[0] == 200
        for _ in range(3):
            assert request("/api/tags", authenticated=False)[0] == 401
        status, retry = request("/api/tags", authenticated=False, forged_ip="198.51.100.9")
        assert status == 429 and retry is not None
        # The global unknown budget remains available; a forged per-IP identity
        # would therefore incorrectly turn this into401 instead of429.
        assert request("/api/tags", authenticated=False, forged_ip="198.51.100.10")[0] == 429

        native = json.dumps({"model": values["ollama_model"], "prompt": "Say ok", "stream": False,
                             "options": {"num_ctx": 512}})
        assert request("/api/generate", native, forged_ip="198.51.100.11")[0] == 200
        path, forwarded, leaked = received[-1]
        assert path == "/api/generate" and not leaked
        assert forwarded["options"]["num_ctx"] == 2048
        assert forwarded["options"]["num_predict"] == 256
        assert forwarded["keep_alive"] == "5m" and forwarded["think"] is False
        assert forwarded["prompt"] == "Say ok"  # Forward-auth did not consume the body.

        count = len(received)
        duplicate = '{"model":"qwen3:4b-q4_K_M","prompt":"a","prompt":"b"}'
        oversized_context = json.dumps({"model": values["ollama_model"], "prompt": "ok",
                                        "options": {"num_ctx": 4096}})
        assert request("/api/generate", duplicate)[0] == 400
        assert request("/api/generate", oversized_context)[0] == 400
        assert len(received) == count

        block_next.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(request, "/api/generate", native)
            try:
                assert entered.wait(timeout=3), "First inference did not reach the application"
                status, retry = request("/api/generate", native)
                assert status == 429 and retry is not None
                assert len(received) == count + 1
            finally:
                release.set()
            assert first.result(timeout=5)[0] == 200
        block_next.clear()
        assert request("/api/generate", native)[0] == 200  # Slot released after completion.
        assert all(not leaked for _, _, leaked in received)

        stop_process(guard)
        assert request("/api/tags")[0] == 502
        assert request("/api/generate", native)[0] == 502
    finally:
        release.set()
        stop_process(gateway)
        stop_process(guard)
        application.shutdown()
        application.server_close()
