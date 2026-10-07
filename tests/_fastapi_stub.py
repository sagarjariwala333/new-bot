"""
Only used when fastapi isn't actually installed (this project's own dev
sandbox lacks network access to pip install it). In a real deployment the
real fastapi is present and this stub is never registered.

2026-09-14: extended to cover FastAPI/APIRouter/Depends/Response plus the
fastapi.responses/fastapi.staticfiles submodules - found, while doing a
thorough re-check, that the original minimal version of this stub (only
Request/HTTPException) meant app/main.py and app/api/__init__.py had NEVER
actually been import-verified by anything, in any prior session either -
only ast.parse'd, which can't catch a missing import, a route referencing
an undefined name, or a decorator used incorrectly. This is now real
enough to actually import both files and exercise every route function at
module load time (see tests/test_app_imports.py).
"""
import sys
import types

try:
    import fastapi  # noqa: F401
except ImportError:
    stub = types.ModuleType("fastapi")

    class Request:
        def __init__(self, cookies=None, method="GET", headers=None):
            self.cookies = cookies or {}
            self.method = method
            self.headers = headers or {}

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(str(detail))

    class Response:
        def __init__(self, content=None, status_code=200, headers=None, media_type=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers or {}
            self.media_type = media_type

    class Depends:
        """Just a marker holding the dependency callable - real FastAPI
        resolves it at request time; for import-time verification we only
        need it to exist as a usable default-argument value/list element."""
        def __init__(self, dependency=None):
            self.dependency = dependency

    class _Route:
        """Mirrors the real fastapi.routing.APIRoute's public shape closely
        enough for import-time verification: .path, .endpoint, .dependencies.
        2026-09-14 fix: the previous version of this stub stored routes as
        plain (path, func, kwargs) TUPLES - test_app_imports.py unpacked
        them that way and passed against this stub, but would have raised
        TypeError against a REAL FastAPI app (whose .routes is a list of
        Route/APIRoute OBJECTS with these attributes, not tuples) - exactly
        what two independent third-party reviews, running against a real
        fastapi install, actually hit. Storing real-shaped objects here
        instead means the SAME test code now works correctly against both
        this stub and the real library, rather than only passing here by
        accident."""
        def __init__(self, path, endpoint, dependencies=None):
            self.path = path
            self.endpoint = endpoint
            self.dependencies = list(dependencies or [])

    def _route_decorator_factory(store):
        def method(path, **kwargs):
            def decorator(func):
                store.append(_Route(path, func, kwargs.get("dependencies")))
                return func
            return decorator
        return method

    class APIRouter:
        def __init__(self, *a, **k):
            self.routes = []
            self.get = _route_decorator_factory(self.routes)
            self.post = _route_decorator_factory(self.routes)
            self.put = _route_decorator_factory(self.routes)
            self.delete = _route_decorator_factory(self.routes)
            self.patch = _route_decorator_factory(self.routes)

    class FastAPI:
        def __init__(self, *a, **k):
            self.init_kwargs = dict(k)   # lets tests check how the app was constructed
            self.routes = []
            self.get = _route_decorator_factory(self.routes)
            self.post = _route_decorator_factory(self.routes)
            self.put = _route_decorator_factory(self.routes)
            self.delete = _route_decorator_factory(self.routes)

        def include_router(self, router, *a, **k):
            self.routes.extend(router.routes)

        def mount(self, path, app, name=None):
            pass

    stub.Request = Request
    stub.HTTPException = HTTPException
    stub.Response = Response
    stub.Depends = Depends
    stub.APIRouter = APIRouter
    stub.FastAPI = FastAPI
    sys.modules["fastapi"] = stub

    # ---- fastapi.responses ----
    responses_stub = types.ModuleType("fastapi.responses")

    class FileResponse(Response):
        def __init__(self, path, *a, **k):
            super().__init__(*a, **k)
            self.path = path

    class JSONResponse(Response):
        def __init__(self, content=None, *a, **k):
            super().__init__(content=content, *a, **k)

    responses_stub.FileResponse = FileResponse
    responses_stub.JSONResponse = JSONResponse
    sys.modules["fastapi.responses"] = responses_stub
    stub.responses = responses_stub

    # ---- fastapi.staticfiles ----
    staticfiles_stub = types.ModuleType("fastapi.staticfiles")

    class StaticFiles:
        def __init__(self, *a, **k):
            pass

    staticfiles_stub.StaticFiles = StaticFiles
    sys.modules["fastapi.staticfiles"] = staticfiles_stub
    stub.staticfiles = staticfiles_stub
