from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from app.config import (
    is_default_node_name,
    load_config,
    random_node_name,
    sanitize_node_name,
    save_config,
)
from app.runtime import NodeService
from app.web.api import create_web_app

_DEVNULL_STREAMS = []


def ensure_stdio() -> None:
    """PyInstaller --noconsole can leave stdio as None; uvicorn expects streams."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        _DEVNULL_STREAMS.append(sys.stdout)
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        _DEVNULL_STREAMS.append(sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC simulator MVP web node")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument(
        "--node-name",
        default=None,
        help="Optional custom node name shown in the Web console and P2P HELLO",
    )
    return parser.parse_args()


def announce(config: dict, service: NodeService) -> None:
    """Print the console URL and the admin token once, at startup.

    The token is what lets a teacher drive this node from another machine.
    It is stored next to the database and deliberately never written into the
    config file, which is tracked by git.
    """
    host = config["web_host"]
    display = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print()
    print(f"  BTC Simulator {config['version']}  node: {config['node_name']}")
    print(f"  console:     http://{display}:{int(config['web_port'])}")
    print(f"  admin token: {service.admin_token}")
    print("  (reads are open on the LAN; writes need this token, or the node's own machine)")
    print(f"  remote admin: http://<this machine>:{int(config['web_port'])}/?token={service.admin_token}")
    print()


def main() -> None:
    ensure_stdio()
    args = parse_args()
    config = load_config(args.config)
    if args.node_name:
        config["node_name"] = sanitize_node_name(args.node_name)
        save_config(config)
    elif is_default_node_name(config.get("node_name")):
        config["node_name"] = random_node_name()
        save_config(config)
    service = NodeService(config)
    app = create_web_app(service)
    announce(config, service)
    uvicorn.run(
        app,
        host=config["web_host"],
        port=int(config["web_port"]),
        log_level="info",
    )


if __name__ == "__main__":
    main()
