#!/usr/bin/env python3
"""Bounded, loopback-only inference capacity acceptance; never log model text."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import json
import math
import re
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from gpu_residency import evaluate_residency


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class AcceptanceFailure(ValueError):
    """Contains only fixed, authored diagnostic text, never remote data."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Acceptance redirects are prohibited")


def validate_config(config):
    target = urlsplit(config["api_url"])
    if (target.scheme != "http" or target.hostname != "127.0.0.1"
            or target.username or target.password or target.query or target.fragment
            or target.path not in ("", "/") or not target.port):
        raise ValueError("Only the private loopback API is supported")
    if type(config["require_gpu"]) is not bool:
        raise ValueError("Explicit accelerator mode required")
    for key in ("context_length", "parallel_requests", "predict_tokens", "timeout"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError("Positive integer settings required")
    if config["parallel_requests"] > 32 or config["context_length"] > 1048576:
        raise ValueError("Acceptance workload exceeds bounded limits")
    fraction = config["min_context_fraction"]
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction < 1:
        raise ValueError("Context coverage must be in (0, 1)")
    if type(config["gpu_headroom_mib"]) is not int or config["gpu_headroom_mib"] < 0:
        raise ValueError("Nonnegative GPU headroom required")
    uuids = config["gpu_device_uuids"]
    if (not isinstance(uuids, list) or len(uuids) != len(set(uuids))
            or any(not isinstance(value, str) or not re.fullmatch(
                r"GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}", value
            ) for value in uuids)):
        raise ValueError("Full unique GPU UUIDs required")
    if not isinstance(config["prompt"], str):
        raise ValueError("Acceptance prompt must be text")
    evaluate_residency([], config["model"], config["gpu_offload_policy"], config["gpu_min_vram_fraction"])


def request_json(config, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(config["api_url"] + path, data=data,
                      headers={"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=config["timeout"]) as response:
        if response.status != 200:
            raise ValueError("Inference HTTP check failed")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Inference response limit exceeded")
    return json.loads(body)


def read_gpu_memory(config):
    args = ["/usr/bin/nvidia-smi", "--query-gpu=uuid,memory.free", "--format=csv,noheader,nounits"]
    if config["gpu_device_uuids"]:
        args.append("--id=" + ",".join(config["gpu_device_uuids"]))
    result = subprocess.run(args, capture_output=True, check=False, timeout=10)
    if result.returncode:
        raise ValueError("GPU memory query failed")
    records = {}
    for row in result.stdout.decode().strip().splitlines():
        uuid, free = [value.strip() for value in row.split(",")]
        if uuid in records or not uuid.startswith("GPU-"):
            raise ValueError("Invalid GPU memory records")
        records[uuid] = int(free)
    if not records or any(value < 0 for value in records.values()):
        raise ValueError("GPU memory data unavailable")
    if config["gpu_device_uuids"] and set(records) != set(config["gpu_device_uuids"]):
        raise ValueError("Selected GPU memory data incomplete")
    return records


def run_acceptance(config, request_fn=None, gpu_reader=None):
    validate_config(config)
    request_fn = request_fn or (lambda path, payload=None: request_json(config, path, payload))
    gpu_reader = gpu_reader or (lambda: read_gpu_memory(config))
    gpu = config["require_gpu"]
    # The generated capacity payload is intentionally close to the configured
    # window. Verify actual tokenizer counts rather than assuming word=tokens.
    words = (
        "The server accepts each request and checks the model memory before work begins. "
        "The queue holds requests while the model reads the input and writes the result. "
        "Each device reports free memory and current load so the service can check capacity. "
        "The test records response time and checks that the model stays on the selected device."
    ).split()
    # Leave room for tokenization, the unique prefix and generation. Oversized
    # prompts can be context-shifted down to half a window by the pinned runner.
    word_count = max(int(config["context_length"] * 0.85) - 32, 1)
    document = " ".join(words[index % len(words)] for index in range(word_count))
    prompt = config["prompt"] or (
        document + "\nReply with the single word READY." if gpu else "Reply with the single word READY."
    )
    payload = {
        "model": config["model"], "prompt": prompt, "stream": False, "think": False,
        # Pin only for this acceptance window; idle unloading remains supported.
        "keep_alive": "5m",
        "options": {"temperature": 0, "num_predict": config["predict_tokens"] if gpu else 16,
                    "num_ctx": config["context_length"]},
    }
    barrier = threading.Barrier(config["parallel_requests"])

    def generate():
        # Different leading text defeats prompt-prefix cache reuse across
        # requests and deployments. This identifier is not an authentication key.
        request_payload = {**payload, "prompt": f"Capacity sample {uuid.uuid4()}:\n{prompt}"}
        barrier.wait(timeout=30)
        return request_fn("/api/generate", request_payload)

    samples = []

    def sample_memory():
        if gpu:
            samples.append(gpu_reader())

    sample_memory()
    with ThreadPoolExecutor(max_workers=config["parallel_requests"]) as pool:
        pending = {pool.submit(generate) for _ in range(config["parallel_requests"])}
        futures = list(pending)
        while pending:
            _, pending = wait(pending, timeout=1)
            sample_memory()
        results = [future.result() for future in futures]

    for result in results:
        if (not isinstance(result, dict) or result.get("done") is not True
                or not isinstance(result.get("response"), str) or not result["response"].strip()
                or type(result.get("eval_count")) is not int
                or not 0 < result["eval_count"] <= payload["options"]["num_predict"]):
            raise AcceptanceFailure("Inference did not generate tokens")
        if (type(result.get("prompt_eval_count")) is not int
                or not 0 <= result["prompt_eval_count"] <= config["context_length"]):
            raise AcceptanceFailure("Invalid prompt token count")
        if gpu and result["prompt_eval_count"] < math.ceil(
                config["context_length"] * config["min_context_fraction"]):
            raise AcceptanceFailure("Acceptance prompt did not exercise the configured context")

    models = request_fn("/api/ps")["models"]
    if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
        raise AcceptanceFailure("Model residency data unavailable")
    selected = [item for item in models if item.get("name") == config["model"]]
    if len(selected) != 1 or selected[0].get("context_length") != config["context_length"]:
        raise AcceptanceFailure("Loaded model context does not match configuration")
    if gpu:
        if evaluate_residency(models, config["model"], config["gpu_offload_policy"],
                              config["gpu_min_vram_fraction"]) != "ok":
            raise AcceptanceFailure("GPU placement policy failed after concurrent inference")
        identities = set(samples[0])
        if any(set(sample) != identities for sample in samples):
            raise AcceptanceFailure("GPU inventory changed during acceptance")
        minimum_free = min(value for sample in samples for value in sample.values())
        if minimum_free < config["gpu_headroom_mib"]:
            raise AcceptanceFailure("Measured GPU free memory is below the configured headroom")
    else:
        if selected[0].get("size_vram") != 0:
            raise AcceptanceFailure("CPU mode unexpectedly used GPU memory")
        minimum_free = None
    return {
        "accepted": True, "requests": len(results), "context_length": config["context_length"],
        "minimum_prompt_tokens": min(result["prompt_eval_count"] for result in results),
        "gpu_policy": config["gpu_offload_policy"] if gpu else "cpu",
        # Sampled at one-second intervals and after completion, not a peak-memory guarantee.
        "minimum_sampled_free_vram_mib": minimum_free,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        with open(args.config, encoding="utf-8") as stream:
            config = json.load(stream)
        validate_config(config)
        if not args.worker:
            # Socket timeouts are idle limits. A separate process gives this
            # whole workload a wall-clock deadline, including slow/dripped HTTP.
            child = subprocess.run(
                [sys.executable, __file__, "--config", args.config, "--worker"],
                capture_output=True, check=False, timeout=config["timeout"],
            )
            if len(child.stdout) > 4096:
                raise AcceptanceFailure("Acceptance result exceeded output limit")
            result = json.loads(child.stdout)
            print(json.dumps(result))
            return 0 if child.returncode == 0 and result.get("accepted") is True else 1
        if config["require_gpu"]:
            # The playbook holds the shared maintenance lock around this whole
            # process. Verify JIT-CDI survives a reload without recreating the
            # already-running container before its inference acceptance.
            reload_result = subprocess.run(
                ["/usr/bin/systemctl", "daemon-reload"], capture_output=True,
                check=False, timeout=30,
            )
            if reload_result.returncode:
                raise AcceptanceFailure("GPU lifecycle daemon reload failed")
        result = run_acceptance(config)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired,
            threading.BrokenBarrierError) as failure:
        # Never include an HTTP response, configured prompt or subprocess output.
        print(json.dumps({"accepted": False, "event": "inference_capacity_acceptance_failed",
                          "check": str(failure) if isinstance(failure, AcceptanceFailure) else
                          "Acceptance wall-clock deadline exceeded" if isinstance(failure, subprocess.TimeoutExpired)
                          else "Acceptance configuration or local probe failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
