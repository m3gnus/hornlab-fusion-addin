"""Serve a real Waveguide Generator app on loopback, for the live-protocol checks.

Used by ``tests/test_wglink_live_against_wg.py`` and by
``tests/fixtures/live-protocol-v1/record_exchanges.py``. Both run only in a
Waveguide Generator checkout's own environment, never in the add-in suite's.

The app is WG's own ``create_app`` served by uvicorn on ``127.0.0.1``, with the
port passed as ``advertised_port`` exactly as WG's launcher does. Its startup is
trimmed to the handlers the live protocol needs -- the capability file and the
live session -- because a full start also prewarms native solver workers and
reconciles the Fusion add-in installation, which a test must never do.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sys
import threading
import time
from typing import Iterator


#: The startup and shutdown handlers WG's own live-session tests run
#: (``server/tests/test_cadlink_live_session.py``, ``STARTUP``/``SHUTDOWN``).
STARTUP_HANDLERS = ("advertise_fusion_delivery_on_startup", "start_live_session")
SHUTDOWN_HANDLERS = ("stop_live_session",)
CHECKOUT_ENV = "WGLINK_WG_CHECKOUT"


def import_wg(checkout: str | os.PathLike[str]) -> None:
    root = str(Path(checkout).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _keep_only(handlers: list, names: tuple[str, ...], phase: str) -> None:
    by_name = {getattr(handler, "__name__", ""): handler for handler in handlers}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise RuntimeError(f"WG has no {phase} handler named {missing}")
    handlers[:] = [by_name[name] for name in names]


@contextmanager
def serving(data_dir: Path, port: int | None = None, *, configure=None) -> Iterator[object]:
    """Yield a started WG application serving ``data_dir`` on loopback.

    ``configure(application)`` runs after ``create_app`` and before startup.
    """

    import tempfile

    import uvicorn
    import server.app as wg_app
    from server.app import create_app

    if not Path(wg_app.FRONTEND_DIST).is_dir():
        # A checkout without a built SPA: WG mounts the folder unconditionally,
        # and nothing here serves the UI, so an empty folder stands in for it.
        wg_app.FRONTEND_DIST = Path(tempfile.mkdtemp(prefix="wg-empty-dist-"))
    port = port or reserve_port()
    application = create_app(data_dir=data_dir, advertised_port=port)
    _keep_only(application.router.on_startup, STARTUP_HANDLERS, "startup")
    _keep_only(application.router.on_shutdown, SHUTDOWN_HANDLERS, "shutdown")
    if configure is not None:
        configure(application)
    config = uvicorn.Config(
        application, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="WGUnderTest", daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("WG did not start serving")
        time.sleep(0.02)
    application.state.test_port = port
    try:
        yield application
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        if thread.is_alive():
            raise RuntimeError("WG did not stop")
