"""Command-line entry point for the local marketplace."""

import argparse
import getpass
import logging
from pathlib import Path

from . import accounts
from .core import ApiError, database, initialize, write_transaction
from .server import CampusServer

DEFAULT_DATABASE = Path("var") / "campus_trade.sqlite3"


def _parser():
    parser = argparse.ArgumentParser(description="环校集市本机原型")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="SQLite 文件")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="启动本机 Web 应用")
    serve.add_argument("--port", type=int, default=8765)
    create = commands.add_parser("create-super", help="交互式创建首个超级管理员")
    create.add_argument("--phone", required=True)
    create.add_argument("--nickname", default="超级管理员")
    return parser


def _create_super(path, arguments):
    password = getpass.getpass("设置密码（至少 8 个字符）: ")
    if password != getpass.getpass("再次输入密码: "):
        raise ApiError(400, "两次输入的密码不一致")
    with database(path) as db, write_transaction(db):
        account = accounts.bootstrap_super(db, arguments.phone, arguments.nickname, password)
    print(f"超级管理员已创建，账号编号 {account['id']}")


def _serve(path, port):
    if not 0 <= port <= 65535:
        raise ApiError(400, "端口必须在 0 至 65535 之间")
    with CampusServer(path, port) as server:
        worker = server.start_scheduler()
        print(f"环校集市运行中：http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop_worker.set()
            worker.join(timeout=10)


def main():
    parser = _parser()
    args = parser.parse_args()
    path = args.database.resolve()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        initialize(path)
        if args.command == "create-super":
            _create_super(path, args)
        else:
            _serve(path, getattr(args, "port", 8765))
    except (ApiError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
