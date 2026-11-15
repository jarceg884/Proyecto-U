"""Spec 045 follow-up (0.7.2): one governance syncer per device per process.

The desktop's gateway serves every profile from one process and loads the governance plugin once
per profile — a separate module copy per profile directory. Each copy used to run its own
BundleSyncer: N syncs/heartbeats per cycle and N writers of the same device files (a shared
``.tmp`` name raced ``os.replace``; on Windows the WinError 2 killed sync threads). These tests
load two REAL copies of the plugin under different module names, exactly like the gateway does,
and check that:

- the first syncer of a device directory leads (talks to the control plane) and the other one
  follows: it mirrors the leader's verified state into its own GovernanceState, never syncing;
- when the leader stops, a follower takes over;
- a cycle that raises does not kill the sync thread;
- concurrent atomic writes of one device file never collide.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes" / "plugins" / "trust-agent-governance"
REGISTRY_KEY = "_trust_agent_governance_syncers"


def _load_copy(name: str):
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        pause.wait(0.05)
    return predicate()


@pytest.fixture
def device_home(tmp_path, monkeypatch):
    home = tmp_path / ".trust-agent"
    (home / "trust-agent").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    sys.modules.pop(REGISTRY_KEY, None)
    yield home
    registry = sys.modules.pop(REGISTRY_KEY, None)
    for syncer in list(getattr(registry, "leaders", {}).values()):
        syncer.stop()


@pytest.fixture
def two_copies(device_home):
    first = _load_copy("trust_agent_governance_copy_a")
    second = _load_copy("trust_agent_governance_copy_b")
    assert first.bundle_client is not second.bundle_client  # two real module copies
    first.state.reset_state()
    second.state.reset_state()
    yield first, second
    for name in ("trust_agent_governance_copy_a", "trust_agent_governance_copy_b"):
        for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            sys.modules.pop(key, None)


def _fake_tick(copy, calls: list, jti: str):
    def tick(self):
        calls.append(time.monotonic())
        payload = {"jti": jti, "exp": time.time() + 3600, "policy_version": 7, "aud": "dev-1"}
        copy.state.get_state().apply_bundle(payload)
        return copy.state.get_state().snapshot()

    return tick


def test_one_leader_per_device_and_the_follower_mirrors_its_state(two_copies, monkeypatch):
    first, second = two_copies
    leader_calls: list = []
    follower_calls: list = []
    monkeypatch.setattr(first.bundle_client.BundleSyncer, "tick", _fake_tick(first, leader_calls, "jti-leader"))
    monkeypatch.setattr(second.bundle_client.BundleSyncer, "tick", _fake_tick(second, follower_calls, "jti-follower"))

    leader = first.bundle_client.BundleSyncer()
    follower = second.bundle_client.BundleSyncer()
    leader.start()
    assert _wait_until(lambda: leader.role == "leader" and leader_calls)
    follower.start()

    def mirrored():
        snap = second.state.get_state().snapshot()
        return snap["mode"] == "normal" and (snap["bundle"] or {}).get("jti") == "jti-leader"

    assert _wait_until(mirrored), second.state.get_state().snapshot()
    assert follower.role == "follower"
    assert follower_calls == []  # the follower never talks to the control plane
    # Only the leader writes the status file the desktop reads.
    status = json.loads((Path(first.bundle_client.trust_dir()) / "governance_status.json").read_text())
    assert status["bundle"]["jti"] == "jti-leader"

    leader.stop()
    follower.stop()


def test_a_follower_takes_over_when_the_leader_stops(two_copies, monkeypatch):
    first, second = two_copies
    leader_calls: list = []
    follower_calls: list = []
    monkeypatch.setattr(first.bundle_client.BundleSyncer, "tick", _fake_tick(first, leader_calls, "jti-a"))
    monkeypatch.setattr(second.bundle_client.BundleSyncer, "tick", _fake_tick(second, follower_calls, "jti-b"))

    leader = first.bundle_client.BundleSyncer()
    follower = second.bundle_client.BundleSyncer()
    leader.start()
    assert _wait_until(lambda: leader_calls)
    follower.start()
    assert _wait_until(lambda: follower.role == "follower")

    leader.stop()
    assert _wait_until(lambda: follower.role == "leader" and follower_calls)
    assert second.state.get_state().snapshot()["bundle"]["jti"] == "jti-b"
    follower.stop()


def test_a_cycle_that_raises_does_not_kill_the_sync_thread(device_home, monkeypatch):
    copy = _load_copy("trust_agent_governance_copy_crash")
    try:
        copy.state.reset_state()
        monkeypatch.setattr(copy.bundle_client, "_SYNC_INTERVAL_FLOOR", 0.05)
        calls: list = []

        def tick(self):
            calls.append(1)
            if len(calls) == 1:
                raise FileNotFoundError("[WinError 2] simulated replace race")
            return {}

        monkeypatch.setattr(copy.bundle_client.BundleSyncer, "tick", tick)
        syncer = copy.bundle_client.BundleSyncer(interval=0.05)
        syncer.start()
        assert _wait_until(lambda: len(calls) >= 2)
        assert syncer.is_running()
        log = (Path(copy.bundle_client.trust_dir()) / "plugin.log").read_text(encoding="utf-8")
        assert "sync_cycle_crashed" in log
        syncer.stop()
    finally:
        for key in [k for k in sys.modules if k.startswith("trust_agent_governance_copy_crash")]:
            sys.modules.pop(key, None)


def test_concurrent_atomic_writes_of_one_device_file_never_collide(two_copies):
    first, _ = two_copies
    target = Path(first.bundle_client.trust_dir()) / "governance-bundle.jws"
    errors: list = []

    def writer(n: int):
        for i in range(40):
            try:
                first.bundle_client.atomic_write_text(target, json.dumps({"writer": n, "i": i}))
            except OSError as exc:  # pragma: no cover - the failure this test guards against
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8"))["i"] == 39
    assert not list(target.parent.glob(".governance-bundle.jws.*.tmp"))
