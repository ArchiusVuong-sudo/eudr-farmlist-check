"""Production entry point: start the embedded PostGIS, then serve the app with waitress on 127.0.0.1.

Usage: uv run python -m farmcheck.serve   (PORT, default 4121; FARMCHECK_DATA_DIR, default var/pgdata)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path
from types import FrameType

from waitress import serve

from . import ROOT
from .app import create_app
from .db import Database
from .mapping.providers import live_available
from .services import Engine


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT", "4121"))
    data_dir = Path(os.environ.get("FARMCHECK_DATA_DIR") or ROOT / "var" / "pgdata")
    db = Database(data_dir)

    def stop(signum: int, _frame: FrameType | None) -> None:
        logging.info("signal %s: stopping the database", signum)
        db.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    db.start()
    try:
        engine = Engine(db)
        live, why = live_available()
        logging.info("mapping proposals: %s (%s)", "live" if live else "replay", why)
        app = create_app(engine)
        logging.info("serving on http://127.0.0.1:%s", port)
        serve(app, host="127.0.0.1", port=port, threads=4, ident="farmcheck")
    finally:
        db.stop()


if __name__ == "__main__":
    main()
