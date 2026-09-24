"""Offline regression tests for the BRAIN QUICK / FULL simulation mode.

Runs with plain CPython (no pytest needed, no network, no credentials):
    python test_simulation_mode.py

The public tools in main.py are wrapped by the mcp 2.x MCPServer decorator at
import time. main.py does not exercise MCPServer anywhere except that decorator,
so a deterministic no-op stand-in is installed before importing it; everything
under test (SimulationSettings, the three entry points, _build_multisim_payload,
the ledger and the options cache) is the real production code.
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

# Keep the import side effects (Redis probe, on-disk store) out of the repo.
os.environ["BRAIN_CACHE_DIR"] = tempfile.mkdtemp(prefix="brain-mode-test-")
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


class FakeSingleClient:
    """Stands in for brain_client on the single-simulation path."""

    def __init__(self):
        self.calls = []

    async def create_simulation(self, simulation_data, reuse_existing=True):
        self.calls.append((simulation_data, reuse_existing))
        return {"id": "A1", "status": "UNSUBMITTED", "settings": simulation_data.settings.model_dump()}


class FakeMultiClient:
    """Stands in for brain_client on both multisimulation paths."""

    def __init__(self):
        self.base_url = "https://example.invalid"
        self.requests = []

    async def ensure_authenticated(self):
        pass

    async def _request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs.get("json")))
        return FakeResponse(status_code=201, headers={"Location": f"{self.base_url}/simulations/42"})


async def _fake_wait_for_multisim(location, expected_children):
    return {"success": True, "alpha_results": []}


def _settings_payload(model):
    # Mirrors BrainApiClient.create_simulation: None values are dropped.
    return {k: v for k, v in model.model_dump().items() if v is not None}


def verify_model_mapping():
    print("\n[1] SimulationSettings maps FULL -> omitted, QUICK -> sent")
    default = main.SimulationSettings()
    explicit_full = main.SimulationSettings(simulationMode="FULL")
    check("default and explicit FULL produce the same request settings",
          _settings_payload(default) == _settings_payload(explicit_full),
          (_settings_payload(default), _settings_payload(explicit_full)))
    check("FULL never puts simulationMode in the settings dict",
          "simulationMode" not in _settings_payload(explicit_full),
          _settings_payload(explicit_full))
    check("QUICK is sent verbatim",
          main.SimulationSettings(simulationMode="QUICK").simulationMode == "QUICK")
    check("mode is case-insensitive (quick)",
          main.SimulationSettings(simulationMode="quick").simulationMode == "QUICK")
    check("mode is case-insensitive (Full -> omitted)",
          main.SimulationSettings(simulationMode=" Full ").simulationMode is None)
    try:
        main.SimulationSettings(simulationMode="TURBO")
        rejected = False
    except ValueError:
        rejected = True
    check("invalid mode is rejected at construction", rejected)

    slim = main._slim_alpha({"id": "A1", "settings": {"region": "USA", "simulationMode": "QUICK"}})
    check("returned alpha settings keep simulationMode unchanged",
          (slim.get("settings") or {}).get("simulationMode") == "QUICK", slim.get("settings"))


async def verify_create_simulation():
    print("\n[2] create_simulation entry point")
    fake = FakeSingleClient()
    original = main.brain_client
    main.brain_client = fake
    try:
        await main.create_simulation(alpha_expression="rank(-returns)", simulation_mode="FULL")
        sim_data, _ = fake.calls[-1]
        check("FULL: no simulationMode in the request settings",
              "simulationMode" not in _settings_payload(sim_data.settings),
              _settings_payload(sim_data.settings))

        await main.create_simulation(alpha_expression="rank(-returns)", simulation_mode="quick")
        sim_data, _ = fake.calls[-1]
        check("QUICK (lowercase): settings.simulationMode == 'QUICK'",
              sim_data.settings.simulationMode == "QUICK", sim_data.settings.simulationMode)

        before = len(fake.calls)
        result = await main.create_simulation(alpha_expression="rank(-returns)", simulation_mode="TURBO")
        check("invalid mode returns an error dict", "error" in result, result)
        check("invalid mode never reaches the client", len(fake.calls) == before, fake.calls)
    finally:
        main.brain_client = original


def verify_build_multisim_payload():
    print("\n[3] _build_multisim_payload")
    args = (["rank(-returns)", "rank(volume)"], "EQUITY", "USA", "TOP3000", 1, 4,
            "INDUSTRY", 0.0, "P0Y0M", "VERIFY", "OFF", "FASTEXPR", None, False, "ON", "OFF")
    full = main._build_multisim_payload(*args)
    check("FULL: key absent on every item",
          all("simulationMode" not in item["settings"] for item in full), full)
    quick = main._build_multisim_payload(*args, simulation_mode="QUICK")
    check("QUICK: key present on every item",
          all(item["settings"].get("simulationMode") == "QUICK" for item in quick), quick)
    try:
        main._build_multisim_payload(*args, simulation_mode="TURBO")
        rejected = False
    except ValueError:
        rejected = True
    check("invalid mode raises", rejected)


async def verify_multi_entry_points():
    print("\n[4] create_multi_simulation / submit_multi_simulation entry points")
    fake = FakeMultiClient()
    original_client = main.brain_client
    original_wait = main._wait_for_multisimulation_completion
    main.brain_client = fake
    main._wait_for_multisimulation_completion = _fake_wait_for_multisim
    try:
        await main.create_multi_simulation(["rank(-returns)", "rank(volume)"], simulation_mode="FULL")
        payload = fake.requests[-1][2]
        check("create_multi FULL: key absent",
              all("simulationMode" not in item["settings"] for item in payload), payload)

        await main.create_multi_simulation(["rank(-returns)", "rank(volume)"], simulation_mode="QUICK")
        payload = fake.requests[-1][2]
        check("create_multi QUICK: key present",
              all(item["settings"].get("simulationMode") == "QUICK" for item in payload), payload)

        before = len(fake.requests)
        result = await main.create_multi_simulation(["rank(-returns)", "rank(volume)"], simulation_mode="TURBO")
        check("create_multi invalid returns error", "error" in result, result)
        check("create_multi invalid sends no request", len(fake.requests) == before, fake.requests)

        await main.submit_multi_simulation(["rank(-returns)", "rank(volume)"], simulation_mode="quick")
        payload = fake.requests[-1][2]
        check("submit_multi QUICK: key present",
              all(item["settings"].get("simulationMode") == "QUICK" for item in payload), payload)

        await main.submit_multi_simulation(["rank(-returns)", "rank(volume)"], simulation_mode="FULL")
        payload = fake.requests[-1][2]
        check("submit_multi FULL: key absent",
              all("simulationMode" not in item["settings"] for item in payload), payload)

        before = len(fake.requests)
        result = await main.submit_multi_simulation(["rank(-returns)", "rank(volume)"], simulation_mode="TURBO")
        check("submit_multi invalid returns error", "error" in result, result)
        check("submit_multi invalid sends no request", len(fake.requests) == before, fake.requests)
    finally:
        main.brain_client = original_client
        main._wait_for_multisimulation_completion = original_wait


class _FakeStore:
    def __init__(self, root):
        self.root = root
        self.puts = []

    async def put(self, namespace, key, row):
        self.puts.append((namespace, key, row))


class _LedgerClient:
    def __init__(self, root):
        self.store = _FakeStore(root)

    def log(self, *a, **k):
        pass


async def verify_fingerprint_and_ledger():
    print("\n[5] ledger fingerprint and rows")
    base = {"type": "REGULAR", "settings": {"region": "USA"}, "regular": "rank(-returns)"}
    quick = {"type": "REGULAR", "settings": {"region": "USA", "simulationMode": "QUICK"},
             "regular": "rank(-returns)"}
    f_full = main.BrainApiClient._simulation_fingerprint(base)
    f_quick = main.BrainApiClient._simulation_fingerprint(quick)
    check("FULL and QUICK cache keys differ", f_full != f_quick, (f_full, f_quick))

    root = Path(tempfile.mkdtemp(prefix="brain-ledger-test-"))
    client = _LedgerClient(root)
    await main.BrainApiClient._ledger_record(client, f_quick, quick, {"id": "A1", "is": {}})
    await main.BrainApiClient._ledger_record(client, f_full, base, {"id": "A2", "is": {}})
    modes = [row["simulation_mode"] for _, _, row in client.store.puts]
    check("ledger rows record QUICK and FULL explicitly", modes == ["QUICK", "FULL"], modes)


class _OptionsResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}
        self.text = "{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _options_payload():
    return {"actions": {"POST": {
        "type": {"choices": [{"value": "REGULAR"}, {"value": "REGION_AGNOSTIC"}]},
        "settings": {"children": {
            "instrumentType": {"type": "choice", "label": "Instrument type",
                               "choices": [{"value": "EQUITY"}]},
            "region": {"type": "choice", "label": "Region",
                       "choices": {"instrumentType": {"EQUITY": [{"value": "USA"}]}}},
            "universe": {"type": "choice", "label": "Universe",
                         "choices": {"instrumentType": {"EQUITY": {"region": {"USA": [{"value": "TOP3000"}]}}}}},
            "delay": {"type": "choice", "label": "Delay",
                      "choices": {"instrumentType": {"EQUITY": {"region": {"USA": [{"value": 1}]}}}}},
            "neutralization": {"type": "choice", "label": "Neutralization",
                               "choices": {"instrumentType": {"EQUITY": {"region": {"USA": [{"value": "NONE"}]}}}}},
            "simulationMode": {"type": "choice", "label": "Simulation mode", "optional": True,
                               "choices": [{"value": "FULL"}, {"value": "QUICK"}]},
        }},
    }}}


class _OptionsClient(main.BrainApiClient):
    def __init__(self):
        self.base_url = "https://example.invalid"
        self.cache = {}
        self.requests = []
        self.log = lambda *a, **k: None

    async def ensure_authenticated(self):
        pass

    def _generate_cache_key(self, prefix, params):
        return prefix

    def _get_cached_data(self, key):
        return self.cache.get(key)

    def _set_cached_data(self, key, data, ttl=0):
        self.cache[key] = data

    async def _request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return _OptionsResponse(_options_payload())


async def verify_options_cache():
    print("\n[6] get_platform_setting_options")
    client = _OptionsClient()
    key = client._generate_cache_key("platform_settings", {})
    # A pre-QUICK cache entry has simulation_types but no simulation_modes.
    client.cache[key] = {"instrument_options": [], "total_combinations": 0,
                         "simulation_types": ["REGULAR"], "from_cache": False}

    first = await client.get_platform_setting_options()
    check("stale entry (no simulation_modes) is refreshed with a live OPTIONS call",
          len(client.requests) == 1, client.requests)
    check("live schema exposes simulation_modes == ['FULL', 'QUICK']",
          first.get("simulation_modes") == ["FULL", "QUICK"], first.get("simulation_modes"))
    check("refreshed read is not from_cache", first.get("from_cache") is False, first.get("from_cache"))

    second = await client.get_platform_setting_options()
    check("entry carrying simulation_modes is served from cache",
          len(client.requests) == 1 and second.get("from_cache") is True,
          (client.requests, second.get("from_cache")))
    check("cached simulation_modes survive the round trip",
          second.get("simulation_modes") == ["FULL", "QUICK"], second.get("simulation_modes"))


async def _run():
    verify_model_mapping()
    await verify_create_simulation()
    verify_build_multisim_payload()
    await verify_multi_entry_points()
    await verify_fingerprint_and_ledger()
    await verify_options_cache()


if __name__ == "__main__":
    asyncio.run(_run())
    print("\n" + ("ALL PASS" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}"))
    sys.exit(1 if FAIL else 0)
