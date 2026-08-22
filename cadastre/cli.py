"""Cadastre command line interface."""

from __future__ import annotations

import argparse
import json

from cadastre import __version__
from cadastre.discovery import DiscoveryError, discover_disks, filter_system_disks
from cadastre.server import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only offline disk inventory")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the local operator UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8741)
    serve.add_argument("--db", default=None)
    serve.add_argument("--simulate-disks", action="store_true", help="use isolated deterministic SIMULATED disks")
    discover = sub.add_parser("discover", help="print live read-only disk metadata")
    discover.add_argument("--include-system", action="store_true")
    args = parser.parse_args()
    if args.command == "serve":
        run(args.host, args.port, args.db, simulated=args.simulate_disks)
    else:
        try:
            result = discover_disks()
        except DiscoveryError as exc:
            parser.error(str(exc))
        disks = filter_system_disks(result.disks, args.include_system)
        print(json.dumps({"disks": disks, "warnings": result.warnings}, indent=2))
