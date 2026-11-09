#!/usr/bin/env python3
"""VPS entry point for isolated Serein room provisioning (M1).

No existing single-instance installation is discovered or modified.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from serein.room_docker import DockerRuntime
from serein.room_manager import DEFAULT_IMAGE, RoomError, RoomManager


def parser_for_rooms():
    parser = argparse.ArgumentParser(
        description="Manage independent Serein rooms in an explicit private directory")
    parser.add_argument("--root", type=Path,
                        default=Path(os.environ.get("SEREIN_MULTIROOM_ROOT",
                            str(ROOT / ".runtime" / "multiroom"))),
                        help="Private multi-room storage root (never an existing Serein install)")
    parser.add_argument("--image", default=os.environ.get(
        "SEREIN_MULTIROOM_IMAGE", DEFAULT_IMAGE))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create an empty room registry")
    commands.add_parser("list", help="List room metadata, excluding secrets")
    make = commands.add_parser("create", help="Create an isolated stopped room")
    make.add_argument("name")
    make.add_argument("--room-id", dest="room_slug", default=None,
                      help="Unique room ID, e.g. xiaogi-main (optional for CLI)")
    source = make.add_mutually_exclusive_group()
    source.add_argument("--blank", action="store_true",
                        help="Start without source configuration")
    source.add_argument("--source", metavar="ROOM_ID",
                        help="Copy configuration from a specified room")
    make.add_argument("--reuse-provider-keys", action="store_true",
                      help="Explicitly copy model-provider credentials from the source room")
    make.add_argument("--show-gateway-key", action="store_true",
                      help="Print the new room key once (never stored in the public registry)")
    commands.add_parser("build", help="Build one shared Docker image from this fork")
    for command in ("show", "status", "start", "stop"):
        item = commands.add_parser(command)
        item.add_argument("room_id")
    commands.choices["start"].add_argument("--wait", type=int, default=45)
    gateway = commands.add_parser("gateway-start",
                                  help="Start the shared Chat/MCP proxy bound to loopback")
    gateway.add_argument("--port", type=int, default=18219)
    commands.add_parser("gateway-stop")
    commands.add_parser("gateway-status")
    commands.add_parser("dashboard-build", help="Build original React/Vite Dashboard image")
    dashboard = commands.add_parser("dashboard-start",
                                     help="Start one private original Dashboard for all rooms")
    dashboard.add_argument("--port", type=int, default=18217)
    dashboard.add_argument("--public-origin", default="",
                           help="HTTPS origin for an operator-configured TLS reverse proxy")
    commands.add_parser("dashboard-status")
    commands.add_parser("dashboard-stop")
    commands.add_parser("bridge-address",
                        help="Show private Docker bridge IP for host admin-serve")
    credentials = commands.add_parser(
        "web-auth", help="Configure Dashboard username and password (interactive)")
    credentials.add_argument("--username", default=None,
                             help="Your preferred Dashboard username; password is prompted securely")
    commands.add_parser("admin-init", help="Generate an independent private administrator key")
    admin = commands.add_parser("admin-serve",
                                help="Start the administrator dashboard on loopback only")
    admin.add_argument("--port", type=int, default=18220)
    admin.add_argument("--docker-network", action="store_true",
                       help="Bind administrator API only to installation's private Docker bridge")
    return parser


def main(argv=None):
    args = parser_for_rooms().parse_args(argv)
    manager = RoomManager(args.root, image=args.image)
    runtime = DockerRuntime()
    if args.command == "init":
        manager.initialize()
        result = {"root": str(manager.root), "registry": str(manager.registry)}
    elif args.command == "list":
        result = {"rooms": manager.list_rooms()}
    elif args.command == "create":
        source = "blank" if args.blank else args.source or "first"
        created = manager.create(args.name, source=source, room_slug=args.room_slug,
                                 reuse_provider_keys=args.reuse_provider_keys)
        result = {"room": created["room"]}
        if args.show_gateway_key:
            result["gateway_key"] = created["gateway_key"]
        else:
            result["key_file"] = str(manager.room_dir(created["room"]["room_id"]) /
                                     "secrets" / "gateway-key")
    elif args.command == "build":
        runtime.build(ROOT, manager.image)
        result = {"image": manager.image}
    elif args.command == "gateway-start":
        result = runtime.start_gateway(manager, port=args.port)
    elif args.command == "gateway-stop":
        result = runtime.stop_gateway(manager)
    elif args.command == "gateway-status":
        manager.initialize()
        result = runtime.gateway_status(manager)
    elif args.command == "dashboard-build":
        runtime.build_dashboard(ROOT)
        result = {"image": runtime.dashboard_image()}
    elif args.command == "dashboard-start":
        result = runtime.start_dashboard(manager, port=args.port,
                                         public_origin=args.public_origin)
    elif args.command == "dashboard-status":
        manager.initialize()
        result = runtime.dashboard_status(manager)
    elif args.command == "dashboard-stop":
        result = runtime.stop_dashboard(manager)
    elif args.command == "bridge-address":
        result = {"address": runtime.network_gateway_ip(manager),
                  "network": manager.network}
    elif args.command == "web-auth":
        from serein.room_admin import save_dashboard_credentials
        username = args.username or input("Dashboard 登录用户名：").strip()
        password = getpass.getpass("Dashboard 登录密码（至少 10 字符）：")
        if getpass.getpass("再输入一遍密码：") != password:
            raise RoomError("The two passwords do not match")
        path = save_dashboard_credentials(manager, username, password)
        result = {"web_auth_file": str(path),
                  "message": "Dashboard account updated; no password or key printed"}
    elif args.command == "admin-init":
        from serein.room_admin import initialize_admin_key
        secret = initialize_admin_key(manager)
        result = {"admin_key_file": str(secret),
                  "message": "Keep private; the administrator key is never a room Gateway Key"}
    elif args.command == "admin-serve":
        from serein.room_admin import create_app, read_admin_key
        if not 1 <= args.port <= 65535:
            raise RoomError("Invalid administrator UI port")
        token = read_admin_key(manager)
        import uvicorn
        address = runtime.network_gateway_ip(manager) if args.docker_network else "127.0.0.1"
        uvicorn.run(create_app(manager, admin_key=token, runtime=runtime),
                    host=address, port=args.port, access_log=False)
        return 0
    elif args.command == "show":
        result = manager.get(args.room_id)
    elif args.command == "status":
        room = manager.get(args.room_id)
        result = {**room, "docker": runtime.status(manager, room)}
    elif args.command == "start":
        result = manager.start(args.room_id, runtime, wait_seconds=args.wait)
    elif args.command == "stop":
        result = manager.stop(args.room_id, runtime)
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RoomError, ValueError, OSError) as error:
        print(f"Room operation failed: {error}", file=sys.stderr)
        sys.exit(1)
