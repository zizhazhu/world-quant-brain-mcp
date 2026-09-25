"""Offline regression tests for the in-flight simulation tracker.

Runs with plain CPython (no pytest needed, no network, no credentials):
    python test_inflight_simulations.py

Same import setup as test_simulation_mode.py: the mcp decorator is stubbed,
BRAIN_CACHE_DIR points at a temp dir, and Redis is pointed at a dead port so
the client degrades cleanly. The real brain_client is used under test with
_request / ensure_authenticated / get_alpha_details / record_alpha_locally /
_resolve_ra_children patched per test, and the store root is repointed at a
fresh temp dir so no test can touch a real ledger or cache.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

# Keep the import side effects (Redis probe, on-disk store) out of the repo.
os.environ["BRAIN_CACHE_DIR"] = tempfile.mkdtemp(prefix="brain-inflight-test-")
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"  # nothing listens here; the client degrades cleanly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _install_mcp_stub():
    try:
        import mcp  # noqa: F401
        import mcp.server  # noqa: F401
    except Exception:
        mcp = types.ModuleType("mcp")
        mcp.__path__ = []
        server = types.ModuleType("mcp.server")
        server.__path__ = []
        mcp.server = server
        sys.modules["mcp"] = mcp
        sys.modules["mcp.server"] = server

    stub = types.ModuleType("mcp.server.mcpserver")

    class MCPServer:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def custom_route(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def run(self, *a, **k):
            pass

    stub.MCPServer = MCPServer
    sys.modules["mcp.server.mcpserver"] = stub


_install_mcp_stub()
import main  # noqa: E402

FAIL = []
BASE = "https://api.worldquantbrain.com"


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"   <-- {extra}"))
    if not cond:
        FAIL.append(label)


class FakeResponse:
    def __init__(self, status_code=201, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


async def _noop_auth():
    pass


async def _fake_alpha_details(alpha_id, force_refresh=False):
    return {"id": alpha_id, "is": {}, "type": "REGULAR", "status": "UNSUBMITTED"}


async def _noop_record(alpha):
    pass


async def _no_children(alpha):
    return None


_PATCHED = ('_request', 'ensure_authenticated', 'get_alpha_details',
            'record_alpha_locally', '_resolve_ra_children')


def _patch_client(handler):
    """Aim the real brain_client at a fake transport and a fresh store root."""
    client = main.brain_client
    client.store = main.PersistentStore(
        Path(tempfile.mkdtemp(prefix="brain-inflight-store-")), client.log)
    saved = {name: getattr(client, name) for name in _PATCHED}
    client._request = handler
    client.ensure_authenticated = _noop_auth
    client.get_alpha_details = _fake_alpha_details
    client.record_alpha_locally = _noop_record
    client._resolve_ra_children = _no_children
    return client, saved


def _restore_client(client, saved):
    for name, fn in saved.items():
        setattr(client, name, fn)


def _entry(sim_id, category='other', region='USA', children=1,
           sim_type='REGULAR', tool='create_simulation'):
    return {
        'id': sim_id,
        'location': f"{BASE}/simulations/{sim_id}",
        'submitted_at': time.time() - 100,
        'tool': tool,
        'type': sim_type,
        'region': region,
        'universe': 'TOP3000',
        'mode': 'FULL',
        'multi': children > 1,
        'children': children,
        'category': category,
    }


def _seed_inflight(client, entries, last_rejection=None):
    path = client._inflight_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"inflight": entries, "last_rejection": last_rejection}), encoding='utf-8')


async def test_create_single_other():
    print("\n[1] create_simulation 201 REGULAR/USA -> one inflight entry")

    async def handler(method, url, **kwargs):
        if method == 'POST':
            return FakeResponse(201, headers={"Location": f"{BASE}/simulations/SIM1"})
        return FakeResponse(200, payload={"alpha": "A1", "status": "COMPLETE"})

    client, saved = _patch_client(handler)
    try:
        result = await main.create_simulation(alpha_expression="rank(-returns)")
        check("tool returns the alpha, not an error", result.get("id") == "A1", result)
        data = client._inflight_read_sync()
        check("one inflight entry recorded", len(data["inflight"]) == 1, data)
        entry = data["inflight"][0]
        check("entry fields fixed at submit time",
              entry.get("id") == "SIM1" and
              entry.get("tool") == "create_simulation" and
              entry.get("type") == "REGULAR" and
              entry.get("region") == "USA" and
              entry.get("universe") == "TOP3000" and
              entry.get("mode") == "FULL" and
              entry.get("multi") is False and
              entry.get("children") == 1 and
              entry.get("category") == "other" and
              entry.get("location") == f"{BASE}/simulations/SIM1", entry)
        check("submitted_at is an epoch float",
              isinstance(entry.get("submitted_at"), float), entry)
    finally:
        _restore_client(client, saved)


async def test_create_region_agnostic():
    print("\n[2] create_simulation REGION_AGNOSTIC -> region_agnostic bucket")

    async def handler(method, url, **kwargs):
        if method == 'POST':
            return FakeResponse(201, headers={"Location": f"{BASE}/simulations/RA1"})
        return FakeResponse(200, payload={"alpha": "A2", "status": "COMPLETE"})

    client, saved = _patch_client(handler)
    try:
        result = await main.create_simulation(
            type="REGION_AGNOSTIC", region="ALL", universe="LARGE", delay=1,
            alpha_expression="rank(-returns)")
        check("tool returns the alpha, not an error", result.get("id") == "A2", result)
        data = client._inflight_read_sync()
        check("one inflight entry recorded", len(data["inflight"]) == 1, data)
        entry = data["inflight"][0]
        check("category is region_agnostic (not glb, despite region ALL rules)",
              entry.get("category") == "region_agnostic", entry)
        check("entry keeps region ALL and type REGION_AGNOSTIC",
              entry.get("region") == "ALL" and entry.get("type") == "REGION_AGNOSTIC", entry)
    finally:
        _restore_client(client, saved)


async def test_submit_multi_glb():
    print("\n[3] submit_multi_simulation GLB x3 -> glb bucket, children 3")

    async def handler(method, url, **kwargs):
        return FakeResponse(201, headers={"Location": f"{BASE}/simulations/MULTI1"})

    client, saved = _patch_client(handler)
    try:
        result = await main.submit_multi_simulation(
            ["rank(-returns)", "rank(volume)", "rank(close)"],
            region="GLB", universe="MINVOL1M")
        check("tool reports submitted", result.get("submitted") is True, result)
        data = client._inflight_read_sync()
        check("one inflight entry recorded", len(data["inflight"]) == 1, data)
        entry = data["inflight"][0]
        check("multi entry fields",
              entry.get("id") == "MULTI1" and
              entry.get("tool") == "submit_multi_simulation" and
              entry.get("category") == "glb" and
              entry.get("multi") is True and
              entry.get("children") == 3, entry)
    finally:
        _restore_client(client, saved)


async def test_rejection_429():
    print("\n[4] 429 records last_rejection + background snapshot")

    async def handler(method, url, **kwargs):
        if method == 'POST':
            return FakeResponse(429, headers={"Retry-After": "30"},
                                text="concurrent simulation limit reached")
        return FakeResponse(404)

    client, saved = _patch_client(handler)
    try:
        result = await main.submit_multi_simulation(["rank(-returns)", "rank(volume)"])
        check("tool still returns the RATE_LIMITED shape",
              result.get("error") == "RATE_LIMITED" and
              result.get("status_code") == 429 and
              result.get("retry_after") == "30", result)
        check("no inflight entry on a rejected POST",
              client._inflight_read_sync()["inflight"] == [])

        tasks = list(client._inflight_tasks)
        check("429 schedules a background snapshot task", len(tasks) >= 1, tasks)
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0)  # let done callbacks settle

        rej = client._inflight_read_sync()["last_rejection"]
        check("last_rejection recorded",
              isinstance(rej, dict) and
              rej.get("http_status") == 429 and
              rej.get("retry_after") == "30" and
              "concurrent simulation limit" in (rej.get("body") or "") and
              rej.get("tool") == "submit_multi_simulation" and
              rej.get("category") == "other" and
              rej.get("children") == 2 and
              isinstance(rej.get("at"), float), rej)
        check("snapshot stored inflight_counts_then + snapshot_checked_at",
              isinstance(rej.get("inflight_counts_then"), dict) and
              "snapshot_checked_at" in rej, rej)
    finally:
        _restore_client(client, saved)


async def test_refresh_removes_finished():
    print("\n[5] refresh_inflight: live GET decides kept vs removed")
    entries = [
        _entry('A', category='glb', region='GLB', children=2),
        _entry('B', category='other'),
        _entry('C', category='other'),
        _entry('D', category='region_agnostic', region='ALL', sim_type='REGION_AGNOSTIC'),
    ]

    async def handler(method, url, **kwargs):
        sid = url.rstrip('/').split('/')[-1]
        if sid == 'A':
            return FakeResponse(200, payload={"status": "RUNNING", "progress": 0.4},
                                headers={"Retry-After": "5"})
        if sid == 'B':
            return FakeResponse(200, payload={"status": "COMPLETE"})
        if sid == 'C':
            return FakeResponse(404)
        raise ConnectionError("simulated network failure")

    client, saved = _patch_client(handler)
    try:
        _seed_inflight(client, entries)
        result = await client.refresh_inflight()
        remaining = {e['id']: e for e in result['inflight']}
        check("running and unchecked entries kept",
              set(remaining) == {'A', 'D'}, result['inflight'])
        removed = {r['id']: r for r in result['removed_this_check']}
        check("finished entries removed with final_status",
              removed.get('B', {}).get('final_status') == 'COMPLETE' and
              removed.get('C', {}).get('final_status') == 'NOT_FOUND', removed)
        check("removed entries gone from the file",
              {e['id'] for e in client._inflight_read_sync()['inflight']} == {'A', 'D'})
        row_a = remaining['A']
        check("running entry carries platform status/progress/check ok",
              row_a.get('platform_status') == 'RUNNING' and
              row_a.get('progress') == 0.4 and
              row_a.get('check') == 'ok' and
              isinstance(row_a.get('running_seconds'), (int, float)), row_a)
        check("failed probe keeps entry, marked unchecked",
              remaining['D'].get('check', '').startswith('unchecked'), remaining['D'])
        counts = result['counts']
        check("counts are per category over remaining entries",
              counts['total'] == {'submissions': 2, 'children': 3} and
              counts['glb'] == {'submissions': 1, 'children': 2} and
              counts['region_agnostic'] == {'submissions': 1, 'children': 1} and
              counts['other'] == {'submissions': 0, 'children': 0}, counts)
    finally:
        _restore_client(client, saved)


async def test_recording_failure_isolated():
    print("\n[6] recording failure never alters the submission path")

    async def handler(method, url, **kwargs):
        if method == 'POST':
            return FakeResponse(201, headers={"Location": f"{BASE}/simulations/SIMX"})
        return FakeResponse(200, payload={"alpha": "A3", "status": "COMPLETE"})

    client, saved = _patch_client(handler)
    def _boom(data):
        raise OSError("disk full")

    original_write = client._inflight_write_sync
    client._inflight_write_sync = _boom
    try:
        result = await main.create_simulation(alpha_expression="rank(-returns)")
        check("create_simulation still returns the alpha",
              result.get("id") == "A3", result)
    finally:
        client._inflight_write_sync = original_write
        _restore_client(client, saved)


async def test_persistence_on_disk():
    print("\n[7] entries are readable straight off disk")

    async def handler(method, url, **kwargs):
        return FakeResponse(201, headers={"Location": f"{BASE}/simulations/P1"})

    client, saved = _patch_client(handler)
    try:
        await client._post_simulation(
            {'type': 'REGULAR',
             'settings': {'region': 'USA', 'universe': 'TOP3000'},
             'regular': 'rank(-returns)'},
            tool='create_simulation')
        path = client._inflight_path()
        check("inflight.json exists next to the ledger location", path.exists(), path)
        raw = json.loads(path.read_text(encoding='utf-8'))
        check("fresh disk read shows the entry",
              isinstance(raw.get('inflight'), list) and
              len(raw['inflight']) == 1 and
              raw['inflight'][0].get('id') == 'P1' and
              raw.get('last_rejection') is None, raw)
        # A corrupt file must read as empty, not raise.
        path.write_text("{not json", encoding='utf-8')
        check("corrupt file reads as empty",
              client._inflight_read_sync() == {"inflight": [], "last_rejection": None})
    finally:
        _restore_client(client, saved)


async def test_concurrency():
    print("\n[8] concurrent submissions lose nothing; mid-GET adds survive refresh")
    counter = iter(range(100))

    async def handler(method, url, **kwargs):
        if method == 'POST':
            return FakeResponse(201, headers={
                "Location": f"{BASE}/simulations/C{next(counter)}"})
        return FakeResponse(200, payload={"status": "COMPLETE"})

    client, saved = _patch_client(handler)
    try:
        payload = {'type': 'REGULAR',
                   'settings': {'region': 'USA', 'universe': 'TOP3000'},
                   'regular': 'rank(-returns)'}
        await asyncio.gather(*[
            client._post_simulation(dict(payload, settings=dict(payload['settings'])),
                                    tool='create_simulation')
            for _ in range(5)])
        ids = [e['id'] for e in client._inflight_read_sync()['inflight']]
        check("5 concurrent submissions -> 5 distinct entries",
              len(ids) == 5 and len(set(ids)) == 5, ids)

        # An entry added while refresh is mid-GET must survive the re-write.
        get_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(method, url, **kwargs):
            if method == 'GET':
                get_started.set()
                await release.wait()
                return FakeResponse(200, payload={"status": "COMPLETE"})
            return FakeResponse(201, headers={"Location": f"{BASE}/simulations/NEWPOST"})

        client._request = slow_handler
        _seed_inflight(client, [_entry('OLD', category='other')])
        refresh_task = asyncio.create_task(client.refresh_inflight())
        await asyncio.wait_for(get_started.wait(), 5)
        await client._inflight_add(_entry('NEW', category='glb', region='GLB'))
        release.set()
        result = await asyncio.wait_for(refresh_task, 5)
        surviving = {e['id'] for e in result['inflight']}
        check("entry added mid-refresh survives the removal write",
              surviving == {'NEW'} and
              result['removed_this_check'][0]['id'] == 'OLD', result)
        check("late entry marked as not queried this round",
              result['inflight'][0]['check'].startswith('unchecked'), result['inflight'])
        check("survivor also persisted to disk",
              [e['id'] for e in client._inflight_read_sync()['inflight']] == ['NEW'])
    finally:
        _restore_client(client, saved)


async def _run():
    await test_create_single_other()
    await test_create_region_agnostic()
    await test_submit_multi_glb()
    await test_rejection_429()
    await test_refresh_removes_finished()
    await test_recording_failure_isolated()
    await test_persistence_on_disk()
    await test_concurrency()


if __name__ == "__main__":
    asyncio.run(_run())
    print("\n" + ("ALL PASS" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}"))
    sys.exit(1 if FAIL else 0)
