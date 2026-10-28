import json
import subprocess

import pytest

from conftest import ROOT, load_module


@pytest.fixture
def xid_collector(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "roles" / "monitoring" / "files"))
    return load_module("gpu_xid_textfile", "roles/monitoring/files/gpu_xid_textfile.py")


def event(timestamp, code=79):
    return json.dumps({"__REALTIME_TIMESTAMP": str(int(timestamp * 1_000_000)),
                       "MESSAGE": f"NVRM: Xid (0000:01:00): {code}, untrusted channel payload"})


def test_recent_xids_are_numeric_window_gauges_without_log_content(xid_collector):
    result = "\n".join(xid_collector.readings("\n".join([event(500), event(1950, 31), event(1980)]), 2000))
    assert "llm_gpu_xid_events_recent 2" in result
    assert "llm_gpu_xid_last_code 79" in result
    assert "llm_gpu_xid_last_seen_timestamp_seconds 1980.000" in result
    assert "untrusted" not in result and "0000:01:00" not in result


def test_old_xids_expire_without_persistent_false_alarm(xid_collector):
    result = "\n".join(xid_collector.readings(event(1000), 2000))
    assert "llm_gpu_xid_events_recent 0" in result
    assert "llm_gpu_xid_last_code 0" in result


def test_modern_pci_prefixed_nvidia_event_is_recognized(xid_collector):
    row = json.dumps({"__REALTIME_TIMESTAMP": "1999000000",
                      "MESSAGE": "NVRM: Xid (PCI:0000:01:00): 79, pid=123, GPU has fallen off the bus."})
    result = "\n".join(xid_collector.readings(row, 2000))
    assert "llm_gpu_xid_events_recent 1" in result
    assert "llm_gpu_xid_last_code 79" in result


@pytest.mark.parametrize("failure", ["missing", "denied", "no_journal", "timeout"])
def test_journal_unavailable_is_unknown_never_zero_errors(xid_collector, monkeypatch, failure):
    def run(args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("journalctl missing")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 1 if failure == "denied" else 0, "", "denied" if failure == "denied" else "")
    monkeypatch.setattr(xid_collector.subprocess, "run", run)
    result = xid_collector.collect()
    assert "llm_gpu_xid_collection_success 0\n" in result
    assert "llm_gpu_xid_events_recent" not in result
    assert "denied" not in result


def test_no_matching_xids_is_healthy_only_with_current_kernel_journal(xid_collector, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args == xid_collector.AVAILABILITY_QUERY:
            assert "--output-fields=__REALTIME_TIMESTAMP" in args
            return subprocess.CompletedProcess(args, 0, '{"__REALTIME_TIMESTAMP":"1000000"}', "")
        assert "--boot=0" in args and "--since=-15min" in args
        assert "_TRANSPORT=kernel" in args and "--grep=NVRM: Xid" in args
        return subprocess.CompletedProcess(args, 1, "", "")
    monkeypatch.setattr(xid_collector.subprocess, "run", run)
    result = xid_collector.collect()
    assert "llm_gpu_xid_collection_success 1\n" in result
    assert "llm_gpu_xid_events_recent 0\n" in result
    assert len(calls) == 2


def test_xid_event_cap_is_reported_without_replaying_every_historical_event(xid_collector):
    result = "\n".join(xid_collector.readings("\n".join([event(1999)] * 257), 2000))
    assert "llm_gpu_xid_window_truncated 1" in result
    assert "llm_gpu_xid_events_recent 257" in result


def test_unrecognized_event_is_not_silently_healthy(xid_collector):
    with pytest.raises(ValueError):
        xid_collector.readings('{"__REALTIME_TIMESTAMP":"1000000","MESSAGE":"NVRM: Xid invalid"}', 2)
