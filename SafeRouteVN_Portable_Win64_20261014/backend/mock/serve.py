"""python -m backend.mock.serve: explicit localhost-only read-only demo."""
import argparse
from pathlib import Path

from .app import create_mock_app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Launch isolated MOCK_DEMO examples; no runtime/authority access")
    parser.add_argument("--enable-read-only-demo", action="store_true", required=True)
    parser.add_argument("--token-file", type=Path, required=True, help="Absolute private file containing a separate mock bearer token")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)
    if not args.token_file.is_absolute() or not args.token_file.is_file():
        parser.error("An absolute existing private mock token file is required")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be within [1,65535]")
    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
        app = create_mock_app(enabled=args.enable_read_only_demo, bearer_token=token)
    except (OSError, ValueError, TypeError):
        parser.error("Mock configuration or pinned example verification failed")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
