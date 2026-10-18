"""Separate foreground worker launcher; background helpers must use CREATE_NO_WINDOW."""
import asyncio
import argparse
import logging
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stop-file', type=Path)
    args = parser.parse_args()
    if args.stop_file is not None and not args.stop_file.is_absolute():
        raise ValueError('Absolute private stop file required')
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.services.offline_guard import install_from_environment
    install_from_environment('compute-worker')
    from backend.services.compute_worker import ComputeWorker
    from backend.services.settings import Settings
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def controlled():
        task = asyncio.create_task(ComputeWorker(Settings.from_environment()).run())
        try:
            while not task.done() and not args.stop_file.exists():
                await asyncio.sleep(0.25)
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(controlled() if args.stop_file is not None else ComputeWorker(Settings.from_environment()).run())
    except KeyboardInterrupt:
        return 0
    except RuntimeError as error:
        if str(error) == "WORKER_ALREADY_RUNNING":
            print("WORKER_ALREADY_RUNNING")
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
