"""Launch with the validated bind address, including loopback-only local identity."""

import uvicorn

from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(create_app(settings=settings), host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
