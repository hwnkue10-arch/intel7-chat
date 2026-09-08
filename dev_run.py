"""Development runner for BambooChat with configurable port, host, and reload options."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# If current environment lacks dependencies, automatically re-execute using .venv if available
venv_python = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        result = subprocess.run([str(venv_python), *sys.argv])
        sys.exit(result.returncode)

import argparse
import uvicorn
from app.config import load_config
from run import first_run


def main() -> None:
    parser = argparse.ArgumentParser(description="BambooChat Development Server")
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=None,
        help="Port to run server on (overrides config file port)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host/IP to bind to (overrides config file bind_host)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on code changes (ideal for development)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("bamboochat.json"),
        help="Path to configuration JSON file (default: bamboochat.json)",
    )

    args = parser.parse_args()
    config_path = args.config.resolve()

    if config_path.exists():
        config = load_config(config_path)
    else:
        config = first_run(config_path)

    os.environ["BAMBOOCHAT_CONFIG"] = str(config_path)

    port = args.port if args.port is not None else config.port
    host = args.host if args.host is not None else config.bind_host

    print(f"\n[DEV SERVER] {config.server_name}")
    print(f"Host: {host}")
    print(f"Port: {port}")
    print(f"Auto-reload: {'ON' if args.reload else 'OFF'}")
    print(f"접속 주소: http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print(f"LAN 접속 주소: http://{host}:{port}\n")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=args.reload,
        ws_max_size=8192,
        ws_max_queue=16,
        ws_per_message_deflate=False,
        limit_concurrency=60,
        server_header=False,
    )


if __name__ == "__main__":
    main()
