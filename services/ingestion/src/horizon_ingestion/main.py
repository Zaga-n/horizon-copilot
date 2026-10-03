"""Launch the independently deployed ingestion API or worker process."""

import argparse
import asyncio

import uvicorn

from horizon_ingestion.bootstrap.app import create_app
from horizon_ingestion.bootstrap.worker import run_worker
from horizon_ingestion.bootstrap.worker_health import check_worker_health
from horizon_ingestion.config.settings import load_settings
from horizon_ingestion.observability.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Horizon document ingestion")
    parser.add_argument(
        "process", choices=("api", "worker", "worker-health"), nargs="?", default="api"
    )
    args = parser.parse_args()
    if args.process == "worker-health":
        raise SystemExit(check_worker_health())
    settings = load_settings()
    configure_logging(
        level=settings.log_level, full_exception_trace=settings.log_full_exception_trace
    )
    if args.process == "worker":
        asyncio.run(run_worker(settings=settings))
    else:
        uvicorn.run(create_app(settings=settings), host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
