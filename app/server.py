"""Container entry point with an explicit IPv4/IPv6 listening socket."""

import argparse
import inspect
import os
import socket

import uvicorn
from uvicorn.supervisors import ChangeReload, Multiprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    workers = 1 if args.reload else int(os.environ.get("WORKERS", "4"))
    if workers < 1:
        parser.error("WORKERS must be positive")
    config = uvicorn.Config(
        "app.main:app", host=host, port=port, workers=workers, reload=args.reload,
        log_level=os.environ.get("LOG_LEVEL", "info"), proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    # Set IPV6_V6ONLY explicitly through create_server. Otherwise a single
    # worker (asyncio) and reload/multi-worker starts can differ in IPv4 support.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.create_server(
        (host, port), family=family, dualstack_ipv6=(host == "::"),
    ) as sock:
        sock.set_inheritable(True)
        server = uvicorn.Server(config)
        if config.should_reload:
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        elif workers > 1:
            # Older supported Uvicorn releases require target; newer ones
            # create their Server internally and no longer accept it.
            kwargs = {"sockets": [sock]}
            if "target" in inspect.signature(Multiprocess).parameters:
                kwargs["target"] = server.run
            Multiprocess(config, **kwargs).run()
        else:
            server.run(sockets=[sock])
            if not server.started:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
