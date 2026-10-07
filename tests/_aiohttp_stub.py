"""
Only used when aiohttp isn't actually installed (e.g. this project's own dev
sandbox). In a real deployment (`pip install -r requirements.txt`), the real
aiohttp is present and this stub is never registered - `import aiohttp`
below succeeds and the whole try/except is a no-op.
"""
import sys
import types

try:
    import aiohttp  # noqa: F401
except ImportError:
    stub = types.ModuleType("aiohttp")

    class _ClientSession:
        def __init__(self, *a, **k):
            pass

    class ClientError(Exception):
        """A real, narrow exception class - not aliased to plain Exception.
        2026-09-14: found this was too broad while adding the ambiguous-
        order-retry fix, which specifically distinguishes network errors
        (aiohttp.ClientError/asyncio.TimeoutError) from a clean BinanceAPIError
        rejection in an outer try/except. Aliasing ClientError to plain
        Exception would have made that except clause also (incorrectly)
        catch BinanceAPIError under test, masking exactly the distinction
        the fix depends on."""

    stub.ClientSession = _ClientSession
    stub.ClientError = ClientError
    stub.ClientTimeout = lambda *a, **k: None
    stub.WSMsgType = types.SimpleNamespace(TEXT=1, ERROR=2, CLOSED=3)
    sys.modules["aiohttp"] = stub
