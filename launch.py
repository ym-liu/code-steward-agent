"""Optional local launcher that adds graceful, browser-requested API shutdown."""
import argparse

import uvicorn

from Services.ShutdownCoordinator import ShutdownAdmissionMiddleware, ShutdownCoordinator
from Services.WorkspaceNavigation import install_workspace_navigation


def create_server(app, services, *, port=8000, server_factory=None):
    """Inject server ownership explicitly instead of signals, PID files or self-kill."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False)
    server = (server_factory or uvicorn.Server)(config)
    coordinator = ShutdownCoordinator(services, lambda: setattr(server, "should_exit", True))
    app.state.shutdown_coordinator = coordinator
    app.add_middleware(ShutdownAdmissionMiddleware, coordinator=coordinator)
    install_workspace_navigation(app)
    return server


def _port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=8000)
    args = parser.parse_args()
    from CodeSteward import app
    from Services.ServiceContainer import services
    create_server(app, services, port=args.port).run()


if __name__ == "__main__":
    main()
