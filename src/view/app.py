from __future__ import annotations

import asyncio
import ctypes
import faulthandler
import importlib
import inspect
import logging
import os
import sys
import warnings
import weakref
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import lru_cache
from io import UnsupportedOperation
from pathlib import Path
from threading import Thread
from types import TracebackType as Traceback
from typing import (Any, Callable, Coroutine, Generic, TextIO, TypeVar,
                    get_type_hints, overload)
from urllib.parse import urlencode

import ujson
import uvicorn
from rich import print
from rich.traceback import install
from typing_extensions import Unpack

from _view import ViewApp

from ._docs import markdown_docs
from ._loader import finalize, load_fs, load_patterns, load_simple
from ._logging import (Internal, Service, UvicornHijack, enter_server,
                       exit_server, format_warnings)
from ._parsers import supply_parsers
from ._util import make_hint
from .config import Config, load_config
from .exceptions import (BadEnvironmentError, ConfigurationError, ViewError,
                         ViewInternalError)
from .logging import _LogArgs, log
from .routing import Route, RouteOrCallable, V, _NoDefault, _NoDefaultType
from .routing import body as body_impl
from .routing import delete, get, options, patch, post, put
from .routing import query as query_impl
from .typing import Callback, DocsType
from .util import enable_debug

get_type_hints = lru_cache(get_type_hints)

__all__ = "App", "new_app", "get_app"

S = TypeVar("S", int, str, dict, bool)
A = TypeVar("A")
T = TypeVar("T")

_ROUTES_WARN_MSG = (
    "routes argument should only be passed when load strategy is manual"
)

B = TypeVar("B", bound=BaseException)


@dataclass()
class TestingResponse:
    message: str
    headers: dict[str, str]
    status: int


def _format_qs(query: dict[str, Any]) -> dict[str, Any]:
    query_str = {}

    for k, v in query.items():
        if isinstance(v, (dict, list)):
            if isinstance(v, dict):
                query_str[k] = ujson.dumps(_format_qs(v))
            else:
                query_str[k] = ujson.dumps(v)
        else:
            query_str[k] = v

    return query_str


class TestingContext:
    def __init__(
        self,
        app: Callable[[Any, Any, Any], Any],
    ) -> None:
        self.app = app
        self._lifespan = asyncio.Queue()
        self._lifespan.put_nowait("lifespan.startup")

    async def start(self):
        async def receive():
            return await self._lifespan.get()

        async def send(obj: dict[str, Any]):
            pass

        await self.app({"type": "lifespan"}, receive, send)

    async def stop(self):
        await self._lifespan.put("lifespan.shutdown")

    async def _request(
        self,
        method: str,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        body_q = asyncio.Queue()
        start = asyncio.Queue()

        async def receive():
            return {
                "body": ujson.dumps(body).encode(),
                "more_body": False,
                "type": "http.request",
            }

        async def send(obj: dict[str, Any]):
            if obj["type"] == "http.response.start":
                await start.put(
                    (
                        {k.decode(): v.decode() for k, v in obj["headers"]},
                        obj["status"],
                    )
                )
            elif obj["type"] == "http.response.body":
                await body_q.put(obj["body"].decode())
            else:
                raise ViewInternalError(f"bad type: {obj['type']}")

        truncated_route = route[: route.find("?")] if "?" in route else route
        query_str = _format_qs(query or {})

        await self.app(
            {
                "type": "http",
                "http_version": "1.1",
                "path": truncated_route,
                "query_string": urlencode(query_str).encode()
                if query
                else b"",  # noqa
                "headers": [],
                "method": method,
            },
            receive,
            send,
        )

        headers, status = await start.get()
        body_s = await body_q.get()

        return TestingResponse(body_s, headers, status)

    async def get(
        self,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        return await self._request("GET", route, body=body, query=query)

    async def post(
        self,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        return await self._request("POST", route, body=body, query=query)

    async def put(
        self,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        return await self._request("PUT", route, body=body, query=query)

    async def patch(
        self,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        return await self._request("PATCH", route, body=body, query=query)

    async def delete(
        self,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        return await self._request("DELETE", route, body=body, query=query)

    async def options(
        self,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestingResponse:
        return await self._request("OPTIONS", route, body=body, query=query)


@dataclass
class InputDoc(Generic[T]):
    desc: str
    type: tuple[type[T], ...]
    default: T | _NoDefaultType


@dataclass
class RouteDoc:
    desc: str
    body: dict[str, InputDoc]
    query: dict[str, InputDoc]


class App(ViewApp):
    """Public view.py app object."""

    def __init__(self, config: Config) -> None:
        """
        Args:
            config: Configuration object to be used. Automatically generated by `new_app`.
        """
        supply_parsers(self)
        self.config = config
        self._set_dev_state(config.dev)
        self._manual_routes: list[Route] = []
        self.routes: list[Route] = []
        self.loaded: bool = False
        self.running = False
        self._docs: DocsType = {}
        self.loaded_routes: list[Route] = []
        self.templaters: dict[str, Any] = {}

        Service.log.setLevel(
            config.log.level
            if not isinstance(config.log.level, str)
            else config.log.level.upper()
        )

        if config.dev:
            if os.environ.get("VIEW_PROD") is not None:
                Service.warning("VIEW_PROD is set but dev is set to true")

            format_warnings()
            weakref.finalize(self, self._finalize)

            if config.log.pretty_tracebacks:
                install(show_locals=True)

            rich_handler = sys.excepthook

            def _hook(tp: type[B], value: B, traceback: Traceback) -> None:
                rich_handler(tp, value, traceback)
                os.environ["_VIEW_CANCEL_FINALIZERS"] = "1"

                if isinstance(value, ViewError):
                    if value.hint:
                        print(value.hint)

            sys.excepthook = _hook
            with suppress(UnsupportedOperation):
                faulthandler.enable()
        else:
            os.environ["VIEW_PROD"] = "1"

        if config.log.level == "debug":
            enable_debug()

        self.running = False

    def _finalize(self) -> None:
        if os.environ.get("_VIEW_CANCEL_FINALIZERS"):
            return

        if self.loaded:
            return

        warnings.warn(
            "load() was never called (did you forget to start the app?)"
        )
        split = self.config.app.app_path.split(":", maxsplit=1)

        if len(split) != 2:
            return

        app_name = split[1]

        print(
            make_hint(
                "Add this to your code",
                split[0],
                line=-1,
                prepend=f"\n{app_name}.run()",
            )
        )

    def _push_route(self, route: Route) -> None:
        if route in self._manual_routes:
            return

        self._manual_routes.append(route)

    def _method_wrapper(
        self,
        path: str,
        doc: str | None,
        cache_rate: int,
        target: Callable[..., Any],
        # i dont really feel like typing this properly
    ) -> Callable[[RouteOrCallable], Route]:
        def inner(route: RouteOrCallable) -> Route:
            new_route = target(path, doc, cache_rate=cache_rate)(route)
            self._push_route(new_route)
            return new_route

        return inner

    def get(self, path: str, doc: str | None = None, *, cache_rate: int = -1):
        """Set a GET route."""
        return self._method_wrapper(path, doc, cache_rate, get)

    def post(self, path: str, doc: str | None = None, *, cache_rate: int = -1):
        """Set a POST route."""
        return self._method_wrapper(path, doc, cache_rate, post)

    def delete(self, path: str, doc: str | None = None, *, cache_rate: int = -1):
        """Set a DELETE route."""
        return self._method_wrapper(path, doc, cache_rate, delete)

    def patch(self, path: str, doc: str | None = None, *, cache_rate: int = -1,):
        """Set a PATCH route."""
        return self._method_wrapper(path, doc, cache_rate, patch)

    def put(self, path: str, doc: str | None = None, *, cache_rate: int = -1):
        """Set a PUT route."""
        return self._method_wrapper(path, doc, cache_rate, put)

    def options(self, path: str, doc: str | None = None, *, cache_rate: int = -1):
        """Set a OPTIONS route."""
        return self._method_wrapper(path, doc, cache_rate, options)

    def _set_log_arg(self, kwargs: _LogArgs, key: str) -> None:
        if key not in kwargs:
            kwargs[key] = getattr(self.config.log.user, key)

    def _splat_log_args(self, kwargs: _LogArgs) -> _LogArgs:
        self._set_log_arg(kwargs, "log_file")
        self._set_log_arg(kwargs, "show_time")
        self._set_log_arg(kwargs, "show_caller")
        self._set_log_arg(kwargs, "show_color")
        self._set_log_arg(kwargs, "show_urgency")
        self._set_log_arg(kwargs, "file_write")
        self._set_log_arg(kwargs, "strftime")

        if "caller_frame" not in kwargs:
            frame = inspect.currentframe()
            assert frame, "failed to get frame"
            back = frame.f_back
            assert back, "frame has no f_back"
            kwargs["caller_frame"] = back

        return kwargs

    def debug(self, *messages: object, **kwargs: Unpack[_LogArgs]) -> None:
        log(*messages, urgency="debug", **self._splat_log_args(kwargs))

    def info(self, *messages: object, **kwargs: Unpack[_LogArgs]) -> None:
        log(*messages, urgency="info", **self._splat_log_args(kwargs))

    def warning(self, *messages: object, **kwargs: Unpack[_LogArgs]) -> None:
        log(*messages, urgency="warning", **self._splat_log_args(kwargs))

    def error(self, *messages: object, **kwargs: Unpack[_LogArgs]) -> None:
        log(*messages, urgency="error", **self._splat_log_args(kwargs))

    def critical(self, *messages: object, **kwargs: Unpack[_LogArgs]) -> None:
        log(*messages, urgency="critical", **self._splat_log_args(kwargs))

    def query(
        self,
        name: str,
        *tps: type[V],
        doc: str | None = None,
        default: V | None | _NoDefaultType = _NoDefault,
    ):
        """Set a query parameter.

        Args:
            name: Name of the parameter.
            tps: Types that can be passed to the server. If empty, any is used.
            doc: Description of this query parameter.
            default: Default value to be used if not supplied.
        """

        def inner(func: RouteOrCallable) -> Route:
            route = query_impl(name, *tps, doc=doc, default=default)(func)
            self._push_route(route)
            return route

        return inner

    def body(
        self,
        name: str,
        *tps: type[V],
        doc: str | None = None,
        default: V | None | _NoDefaultType = _NoDefault,
    ):
        """Set a body parameter.

        Args:
            name: Name of the parameter.
            tps: Types that can be passed to the server. If empty, any is used.
            doc: Description of this body parameter.
            default: Default value to be used if not supplied.
        """

        def inner(func: RouteOrCallable) -> Route:
            route = body_impl(name, *tps, doc=doc, default=default)(func)
            self._push_route(route)
            return route

        return inner

    async def _app(self, scope, receive, send) -> None:
        return await self.asgi_app_entry(scope, receive, send)

    def load(self, routes: list[Route] | None = None) -> None:
        """Load the app. This is automatically called most of the time and should only be called manually during manual loading.

        Args:
            routes: Routes to load into the app.
        """
        if self.loaded:
            if routes:
                finalize(routes, self)
            Internal.warning("load called twice")
            return

        if routes and (self.config.app.loader != "manual"):
            warnings.warn(_ROUTES_WARN_MSG)

        if self.config.app.loader == "filesystem":
            load_fs(self, self.config.app.loader_path)
        elif self.config.app.loader == "simple":
            load_simple(self, self.config.app.loader_path)
        elif self.config.app.loader == "patterns":
            load_patterns(self, self.config.app.loader_path)
        else:
            finalize([*(routes or ()), *self._manual_routes], self)

        self.loaded = True

        for r in self.loaded_routes:
            if not r.path:
                continue

            body = {}
            query = {}

            for i in r.inputs:
                target = body if i.is_body else query
                target[i.name] = InputDoc(
                    i.doc or "No description provided.", i.tp, i.default
                )

            self._docs[(r.method.name, r.path)] = RouteDoc(
                r.doc or "No description provided.", body, query
            )

    async def _spawn(self, coro: Coroutine[Any, Any, Any]):
        Internal.info(f"using event loop: {asyncio.get_event_loop()}")
        Internal.info(f"spawning {coro}")

        task = asyncio.create_task(coro)
        if self.config.log.hijack:
            if self.config.server.backend == "uvicorn":
                Internal.info("hijacking uvicorn")
                for log in (
                    logging.getLogger("uvicorn.error"),
                    logging.getLogger("uvicorn.access"),
                ):
                    log.addFilter(UvicornHijack())
            else:
                Internal.info("hijacking hypercorn")

        if self.config.log.fancy:
            if not self.config.log.hijack:
                raise ConfigurationError(
                    "hijack must be enabled for fancy mode"
                )

            enter_server()

        self.running = True
        Internal.debug("here we go!")
        await task
        self.running = False

        if self.config.log.fancy:
            exit_server()

        Internal.info("server closed")

    def _run(self, start_target: Callable[..., Any] | None = None) -> Any:
        self.load()
        Internal.info("starting server!")
        server = self.config.server.backend
        uvloop_enabled = False

        if self.config.app.uvloop is True:
            uvloop = importlib.import_module("uvloop")
            uvloop.install()
            uvloop_enabled = True
        elif self.config.app.uvloop == "decide":
            with suppress(ModuleNotFoundError):
                uvloop = importlib.import_module("uvloop")
                uvloop.install()
                uvloop_enabled = True

        start = start_target or asyncio.run

        if server == "uvicorn":
            config = uvicorn.Config(
                self._app,
                port=self.config.server.port,
                host=str(self.config.server.host),
                log_level="debug" if self.config.dev else "info",
                lifespan="on",
                factory=False,
                interface="asgi3",
                loop="uvloop" if uvloop_enabled else "asyncio",
                **self.config.server.extra_args,
            )
            server = uvicorn.Server(config)

            return start(self._spawn(server.serve()))

        elif server == "hypercorn":
            raise NotImplementedError
            conf = hypercorn.Config()
            conf.loglevel = "debug" if self.config.dev else "info"
            conf.bind = [
                f"{self.config.server.host}:{self.config.server.port}",
            ]

            for k, v in self.config.server.extra_args.items():
                setattr(conf, k, v)

            return start(
                importlib.import_module("hypercorn.asyncio").serve(
                    self._app, conf
                )
            )
        else:
            raise NotImplementedError("viewserver is not implemented yet")

    def run(self, *, fancy: bool | None = None) -> None:
        """Run the app."""
        if fancy is not None:
            self.config.log.fancy = fancy

        frame = inspect.currentframe()
        assert frame, "failed to get frame"
        assert frame.f_back, "frame has no f_back"

        back = frame.f_back
        base = os.path.basename(back.f_code.co_filename)
        app_path = self.config.app.app_path
        fname = app_path.split(":")[0]
        if base != fname:
            warnings.warn(
                f"ran app from {base}, but app path is {fname} in config",
            )

        if (not os.environ.get("_VIEW_RUN")) and (
            back.f_globals.get("__name__") == "__main__"
        ):
            self._run()
        else:
            Internal.info("called run, but env or scope prevented startup")

    def run_threaded(self, *, daemon: bool = True) -> Thread:
        """Run the app in a thread."""
        thread = Thread(target=self._run, daemon=daemon)
        thread.start()
        return thread

    def run_async(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Run the app in an event loop."""
        self._run((loop or asyncio.get_event_loop()).run_until_complete)

    def run_task(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Task[None]:
        """Run the app as a task."""
        return self._run((loop or asyncio.get_event_loop()).create_task)

    start = run

    def __repr__(self) -> str:
        return f"App(config={self.config!r})"

    @asynccontextmanager
    async def test(self):
        """Open the testing context."""
        self.load()
        ctx = TestingContext(self.asgi_app_entry)
        try:
            yield ctx
        finally:
            await ctx.stop()

    @overload
    def docs(self, file: None = None) -> str:
        ...

    @overload
    def docs(self, file: TextIO) -> None:
        ...

    @overload
    def docs(
        self,
        file: Path,
        *,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> None:
        ...

    @overload
    def docs(
        self,
        file: str,
        *,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> None:
        ...

    def docs(
        self,
        file: str | TextIO | Path | None = None,
        *,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> str | None:
        """Generate documentation for the app."""
        self.load()
        md = markdown_docs(self._docs)

        if not file:
            return md

        if isinstance(file, str):
            if not overwrite:
                Path(file).write_text(md, encoding=encoding)
            else:
                with open(file, "w", encoding=encoding) as f:
                    f.write(md)
        elif isinstance(file, Path):
            if overwrite:
                with open(file, "w", encoding=encoding) as f:
                    f.write(md)
            else:
                file.write_text(md)
        else:
            file.write(md)


def new_app(
    *,
    start: bool = False,
    config_path: Path | str | None = None,
    config_directory: Path | str | None = None,
    post_init: Callback | None = None,
    app_dealloc: Callback | None = None,
    store_address: bool = True,
) -> App:
    """Create a new view app.

    Args:
        start: Should the app be started automatically? (In a new thread)
        config_path: Path of the target configuration file
        config_directory: Directory path to search for a configuration
        post_init: Callback to run after the App instance has been created
        app_dealloc: Callback to run when the App instance is freed from memory
        store_address: Whether to store the address of the instance to allow use from get_app
    """
    config = load_config(
        path=Path(config_path) if config_path else None,
        directory=Path(config_directory) if config_directory else None,
    )

    app = App(config)

    if post_init:
        post_init()

    if start:
        app.run_threaded()

    def finalizer():
        if "_VIEW_APP_ADDRESS" in os.environ:
            del os.environ["_VIEW_APP_ADDRESS"]

        if app_dealloc:
            app_dealloc()

    weakref.finalize(app, finalizer)

    if store_address:
        os.environ["_VIEW_APP_ADDRESS"] = str(id(app))
        # id() on cpython returns the address, but it is
        # implementation dependent however, view.py
        # only supports cpython anyway

    return app


ctypes.pythonapi.Py_IncRef.argtypes = (ctypes.py_object,)


def get_app(*, address: int | None = None) -> App:
    """Get the last app created by `new_app`."""
    env = os.environ.get("_VIEW_APP_ADDRESS")
    addr = address or env

    if (not addr) and (not env):
        raise BadEnvironmentError("no view app registered")

    app: App = ctypes.cast(int(addr), ctypes.py_object).value  # type: ignore
    ctypes.pythonapi.Py_IncRef(app)
    return app

