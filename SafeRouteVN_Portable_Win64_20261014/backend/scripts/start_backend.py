"""Run with the backend venv; no PowerShell execution-policy changes needed."""
import argparse
import asyncio
import logging
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from backend.services.offline_guard import install_from_environment
    install_from_environment('http-api')
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import uvicorn
    if args.stop_file is None:
        uvicorn.run("backend.api.main:app", host=args.host, port=args.port, access_log=False)
    else:
        if not args.stop_file.is_absolute():
            raise ValueError('Absolute private stop file required')

        async def controlled():
            server = uvicorn.Server(uvicorn.Config('backend.api.main:app', host=args.host, port=args.port, access_log=False))
            task = asyncio.create_task(server.serve())
            try:
                while not task.done() and not args.stop_file.exists():
                    await asyncio.sleep(0.25)
                server.should_exit = True
                await task
            finally:
                if not task.done():
                    server.should_exit = True
                    await task

        asyncio.run(controlled())


if __name__ == "__main__":
    main()
