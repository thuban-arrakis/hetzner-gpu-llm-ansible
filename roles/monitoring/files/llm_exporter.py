#!/usr/bin/env python3
"""Cached, loopback-only Ollama metrics with bounded synthetic inference.

Counters describe this exporter's probes, never application-user traffic.
Only fixed metric names and the validated configured model/mode are exported.
Prompts, generated text, API errors and arbitrary response fields are discarded.
"""

import argparse
import json
import math
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULTS = {
    "api_url": "http://127.0.0.1:11434",
    "listen_host": "127.0.0.1",
    "listen_port": 9400,
    "model": "qwen3:4b-q4_K_M",
    "accelerator": "cpu",
    "context_length": 2048,
    "health_interval_seconds": 30,
    "inference_interval_seconds": 300,
    "request_timeout_seconds": 10,
    "inference_timeout_seconds": 120,
}

# Fixed series: do not add response text, request paths, errors or discovered
# models as labels. Only llm_exporter_info carries two configured static labels.
METRICS = {
    "llm_api_up": ("gauge", "Both local model catalogue and residency endpoints are reachable.", 0),
    "llm_model_available": ("gauge", "The configured model is present in the local catalogue.", 0),
    "llm_model_loaded": ("gauge", "The configured model is currently reported by Ollama api/ps.", 0),
    "llm_model_resident_bytes": ("gauge", "Configured loaded model size reported by api/ps; not process RSS.", math.nan),
    "llm_model_vram_bytes": ("gauge", "Configured loaded model size_vram reported by api/ps.", math.nan),
    "llm_model_cpu_bytes": ("gauge", "Estimated CPU model allocation: max(api/ps size minus size_vram, zero).", math.nan),
    "llm_health_probe_requests_total": ("counter", "Health collection attempts made by this exporter.", 0),
    "llm_health_probe_errors_total": ("counter", "Failed health collection attempts.", 0),
    "llm_health_probe_timeouts_total": ("counter", "Health collections exceeding a request or process deadline.", 0),
    "llm_health_last_success_timestamp_seconds": ("gauge", "Unix timestamp of the last complete health collection.", 0),
    "llm_inference_probe_success": ("gauge", "Whether the last synthetic inference generated valid tokens.", 0),
    "llm_inference_probe_requests_total": ("counter", "Synthetic requests made by this exporter, excluding user traffic.", 0),
    "llm_inference_probe_errors_total": ("counter", "Failed synthetic inference requests, excluding user traffic.", 0),
    "llm_inference_probe_timeouts_total": ("counter", "Synthetic inference requests exceeding a request or process deadline.", 0),
    "llm_inference_probe_duration_seconds": ("gauge", "Wall time of the last synthetic request, including failures.", math.nan),
    "llm_inference_probe_tokens_per_second": ("gauge", "Output eval_count divided by eval_duration for the last successful probe.", math.nan),
    "llm_inference_probe_output_tokens": ("gauge", "Output eval_count of the last successful synthetic request.", math.nan),
    "llm_inference_last_success_timestamp_seconds": ("gauge", "Unix timestamp of the last successful synthetic inference.", 0),
    "llm_inference_probe_in_progress": ("gauge", "One when this exporter has an inference probe in progress.", 0),
}


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Exporter configuration contains unsupported fields")
    config = {**DEFAULTS, **raw}
    target = urlsplit(config["api_url"])
    if (
        target.scheme != "http" or target.hostname != "127.0.0.1"
        or target.username or target.password or target.query or target.fragment
        or target.path not in ("", "/") or not target.port
    ):
        raise ValueError("Only the unauthenticated loopback Ollama API is supported")
    config["api_url"] = config["api_url"].rstrip("/")
    if config["listen_host"] != "127.0.0.1":
        raise ValueError("Metrics must bind to IPv4 loopback")
    if type(config["listen_port"]) is not int or not 1024 <= config["listen_port"] <= 65535:
        raise ValueError("Invalid metrics port")
    if config["accelerator"] not in ("cpu", "nvidia"):
        raise ValueError("Unsupported accelerator")
    if not isinstance(config["model"], str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9._/+-]{0,150}:[a-zA-Z0-9._+-]{1,50}", config["model"]
    ):
        raise ValueError("Use one explicit, safe model tag")
    ranges = {
        "context_length": (512, 32768),
        "health_interval_seconds": (15, 300),
        "inference_interval_seconds": (300, 86400),
        "request_timeout_seconds": (1, 30),
        "inference_timeout_seconds": (5, 180),
    }
    for name, (minimum, maximum) in ranges.items():
        if type(config[name]) is not int or not minimum <= config[name] <= maximum:
            raise ValueError("Exporter interval or timeout is outside its safe range")
    return config


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(config, path, body=None):
    if path not in ("/api/tags", "/api/ps", "/api/generate"):
        raise ValueError("Unsupported probe endpoint")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(config["api_url"] + path, data=data, headers={"Content-Type": "application/json"})
    timeout = config["request_timeout_seconds"] if body is None else config["inference_timeout_seconds"]
    # Environment proxies and redirects cannot change the approved destination.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError("Unexpected probe status")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except URLError as failure:
        if isinstance(failure.reason, TimeoutError):
            raise TimeoutError("Probe timed out") from None
        raise
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Probe response exceeds the size limit")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Probe response is not an object")
    return parsed


def nonnegative_integer(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Expected a nonnegative integer")
    return value


def collect_health(config, request=request_json):
    catalogue = request(config, "/api/tags").get("models")
    resident = request(config, "/api/ps").get("models")
    if not isinstance(catalogue, list) or not isinstance(resident, list):
        raise ValueError("Missing model list")
    matching = [item for item in resident if isinstance(item, dict) and item.get("name") == config["model"]]
    if len(matching) > 1:
        raise ValueError("Ambiguous model residency")
    size = nonnegative_integer(matching[0]["size"]) if matching else 0
    vram = nonnegative_integer(matching[0]["size_vram"]) if matching else 0
    return {
        "available": int(any(isinstance(item, dict) and item.get("name") == config["model"] for item in catalogue)),
        "loaded": int(bool(matching)),
        "resident_bytes": size,
        "vram_bytes": vram,
        "cpu_bytes": max(size - vram, 0),
    }


def collect_inference(config, request=request_json):
    response = request(config, "/api/generate", {
        "model": config["model"],
        "prompt": "Reply with only ok.",
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "options": {"num_predict": 8, "num_ctx": config["context_length"], "temperature": 0},
    })
    count = nonnegative_integer(response.get("eval_count"))
    duration = nonnegative_integer(response.get("eval_duration"))
    if (
        response.get("done") is not True or not 1 <= count <= 8 or duration == 0
        or not isinstance(response.get("response"), str) or not response["response"].strip()
    ):
        raise ValueError("Inference did not return valid bounded output")
    # Drop generated text and all unrecognized response fields here.
    return {"output_tokens": count, "tokens_per_second": count * 1_000_000_000 / duration}


def validate_result(kind, result):
    keys = {"available", "loaded", "resident_bytes", "vram_bytes", "cpu_bytes"} if kind == "health" else {
        "output_tokens", "tokens_per_second",
    }
    if not isinstance(result, dict) or set(result) != keys:
        raise ValueError("Invalid probe result")
    for value in result.values():
        if type(value) not in (int, float) or not 0 <= value <= 2**63 - 1 or not math.isfinite(value):
            raise ValueError("Probe metrics must be finite and nonnegative")
    if kind == "health" and (result["available"] not in (0, 1) or result["loaded"] not in (0, 1)):
        raise ValueError("Invalid health result")
    if kind == "inference" and not 1 <= result["output_tokens"] <= 8:
        raise ValueError("Invalid inference token count")
    return result


class Exporter:
    def __init__(self, config, config_path=None, runner=None, clock=time.time, monotonic=time.monotonic):
        self.config = validate_config(config)
        self.config_path = config_path
        self.runner = runner or self._run_child
        self.clock = clock
        self.monotonic = monotonic
        self.metrics = {name: spec[2] for name, spec in METRICS.items()}
        self._state_lock = threading.Lock()
        self._probe_locks = {"health": threading.Lock(), "inference": threading.Lock()}
        self._stop = threading.Event()
        self._threads = []

    def _run_child(self, kind):
        if not self.config_path:
            raise ValueError("A protected service configuration reference is required")
        # A child process provides a hard wall-clock limit even if a local HTTP
        # peer drips bytes forever; socket timeouts alone are only idle limits.
        timeout = (2 * self.config["request_timeout_seconds"] if kind == "health" else
                   self.config["inference_timeout_seconds"]) + 2
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--config", self.config_path, "--probe", kind],
            capture_output=True, timeout=timeout, check=False,
        )
        if result.returncode == 2:
            raise subprocess.TimeoutExpired("local-probe", timeout)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError("Probe child failed")
        return json.loads(result.stdout)

    def run_probe(self, kind):
        if kind not in self._probe_locks:
            raise ValueError("Unknown probe kind")
        if not self._probe_locks[kind].acquire(blocking=False):
            return False
        prefix = "llm_health_probe" if kind == "health" else "llm_inference_probe"
        started = self.monotonic()
        with self._state_lock:
            self.metrics[prefix + "_requests_total"] += 1
            if kind == "inference":
                self.metrics["llm_inference_probe_in_progress"] = 1
        try:
            result = validate_result(kind, self.runner(kind))
            with self._state_lock:
                if kind == "health":
                    self.metrics.update({
                        "llm_api_up": 1,
                        "llm_model_available": result["available"],
                        "llm_model_loaded": result["loaded"],
                        "llm_model_resident_bytes": result["resident_bytes"],
                        "llm_model_vram_bytes": result["vram_bytes"],
                        "llm_model_cpu_bytes": result["cpu_bytes"],
                        "llm_health_last_success_timestamp_seconds": self.clock(),
                    })
                else:
                    self.metrics.update({
                        "llm_inference_probe_success": 1,
                        "llm_inference_probe_output_tokens": result["output_tokens"],
                        "llm_inference_probe_tokens_per_second": result["tokens_per_second"],
                        "llm_inference_last_success_timestamp_seconds": self.clock(),
                    })
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as failure:
            with self._state_lock:
                self.metrics[prefix + "_errors_total"] += 1
                if isinstance(failure, subprocess.TimeoutExpired):
                    self.metrics[prefix + "_timeouts_total"] += 1
                if kind == "health":
                    self.metrics.update({
                        "llm_api_up": 0, "llm_model_available": 0, "llm_model_loaded": 0,
                        "llm_model_resident_bytes": math.nan, "llm_model_vram_bytes": math.nan,
                        "llm_model_cpu_bytes": math.nan,
                    })
                else:
                    self.metrics.update({
                        "llm_inference_probe_success": 0,
                        "llm_inference_probe_tokens_per_second": math.nan,
                        "llm_inference_probe_output_tokens": math.nan,
                    })
        finally:
            with self._state_lock:
                if kind == "inference":
                    self.metrics["llm_inference_probe_duration_seconds"] = max(0, self.monotonic() - started)
                    self.metrics["llm_inference_probe_in_progress"] = 0
            self._probe_locks[kind].release()
        return True

    def _worker(self, kind):
        interval = self.config[kind + "_interval_seconds"]
        while not self._stop.is_set():
            started = self.monotonic()
            self.run_probe(kind)
            # One worker per kind; probes never overlap or catch up in a burst.
            self._stop.wait(max(1, interval - (self.monotonic() - started)))

    def start(self):
        if self._threads:
            raise RuntimeError("Exporter workers are already started")
        for kind in ("health", "inference"):
            worker = threading.Thread(target=self._worker, args=(kind,), daemon=True)
            self._threads.append(worker)
            worker.start()

    def stop(self):
        self._stop.set()
        for worker in self._threads:
            worker.join(timeout=2)

    def render(self):
        with self._state_lock:
            snapshot = dict(self.metrics)
        lines = [
            "# HELP llm_exporter_info Configured local model and inference accelerator.",
            "# TYPE llm_exporter_info gauge",
            f'llm_exporter_info{{accelerator="{self.config["accelerator"]}",model="{self.config["model"]}"}} 1',
        ]
        for name, value in snapshot.items():
            kind, description, _ = METRICS[name]
            rendered = "NaN" if math.isnan(value) else format(value, ".17g")
            lines.extend((f"# HELP {name} {description}", f"# TYPE {name} {kind}", f"{name} {rendered}"))
        return ("\n".join(lines) + "\n").encode("ascii")


def make_handler(exporter):
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            payload = exporter.render()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return MetricsHandler


def load_config(path):
    with open(path, "rb") as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("Configuration exceeds the size limit")
    return validate_config(json.loads(raw))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--probe", choices=("health", "inference"))
    args = parser.parse_args()
    config = load_config(args.config)
    if args.probe:
        result = collect_health(config) if args.probe == "health" else collect_inference(config)
        print(json.dumps(validate_result(args.probe, result), allow_nan=False))
        return 0
    exporter = Exporter(config, str(Path(args.config).resolve()))
    server = ThreadingHTTPServer((config["listen_host"], config["listen_port"]), make_handler(exporter))
    server.daemon_threads = True
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    exporter.start()
    try:
        stopped.wait()
    finally:
        exporter.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError:
        sys.exit(2)
    except Exception:
        # No exception, HTTP body, prompt or generated response reaches logs.
        sys.exit(1)
