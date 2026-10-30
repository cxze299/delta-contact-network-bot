from __future__ import annotations

import argparse
import logging
import os
import sys

from deltachat2 import Rpc
from deltachat2.transport import IOTransport, JsonRpcError

from .config import Settings
from .database import Database
from .delta_adapter import DeltaAdapter
from .service import BotService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delta Chat 好友助手")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="初始化或迁移 SQLite 数据库")
    sub.add_parser("cleanup", help="执行数据保留期限清理")
    seed = sub.add_parser("seed-network", help="从环境变量导入初始网络码")
    seed.add_argument("--name", default="联系人网络", help="网络名称")
    configure = sub.add_parser("configure", help="使用账号 QR 配置机器人账号")
    configure.add_argument("account_qr", help="dcaccount: 或 dclogin: 配置内容")
    sub.add_parser("run", help="启动机器人")
    sub.add_parser("probe-vcard", help="验证 Core 能否为现有联系人生成名片")
    admin_web = sub.add_parser("admin-web", help="启动仅供本地访问的管理后台")
    admin_web.add_argument("--host", default=None)
    admin_web.add_argument("--port", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(settings.database)
    database.initialize()
    if args.command == "init-db":
        print(f"数据库已初始化：{settings.database}")
        return 0
    if args.command == "cleanup":
        result = BotService(database, settings.secret).cleanup_retention()
        print("清理完成：" + ", ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    if args.command == "seed-network":
        code = os.environ.get("CONTACTBOT_INITIAL_NETWORK_CODE") or os.environ.get(
            "INITIAL_NETWORK_CODE"
        )
        if not code:
            raise RuntimeError(
                "请通过 CONTACTBOT_INITIAL_NETWORK_CODE 或 INITIAL_NETWORK_CODE 提供网络码"
            )
        public_id, created = BotService(database, settings.secret).seed_network(args.name, code)
        print(f"网络 {public_id} {'已创建' if created else '已存在'}")
        return 0
    if args.command == "admin-web":
        from .admin_web import run_admin_web

        run_admin_web(
            settings,
            host=args.host or os.environ.get("CONTACTBOT_ADMIN_WEB_HOST", "127.0.0.1"),
            port=args.port or int(os.environ.get("CONTACTBOT_ADMIN_WEB_PORT", "8787")),
        )
        return 0

    settings.accounts_dir.mkdir(parents=True, exist_ok=True)
    # RPC Server 2.59 used by the existing NAS deployment stores its account
    # manager in the process working directory. Passing DC_ACCOUNTS_PATH makes
    # that version try to initialize the already populated directory again.
    with IOTransport(
        rpc_executable=settings.rpc_server, cwd=str(settings.accounts_dir)
    ) as transport:
        rpc = Rpc(transport)
        account_id = _resolve_account(rpc, settings.account_id)
        if args.command == "configure":
            if rpc.is_configured(account_id):
                raise RuntimeError("机器人账号已经配置")
            rpc.add_transport_from_qr(account_id, args.account_qr)
            rpc.set_config(account_id, "displayname", settings.display_name)
            address = rpc.get_config(account_id, "addr")
            print(f"账号 {account_id} 配置完成：{address}")
            try:
                invite = rpc.get_chat_securejoin_qr_code(account_id, None)
                if invite.startswith("OPENPGP4FPR:"):
                    print("机器人添加链接：https://i.delta.chat/#" + invite.removeprefix("OPENPGP4FPR:"))
            except JsonRpcError:
                logging.getLogger("contactbot.cli").warning("暂时无法生成机器人添加链接")
            return 0
        if not rpc.is_configured(account_id):
            raise RuntimeError("机器人账号尚未配置，请先运行 contactbot configure <账号QR>")
        if rpc.get_config(account_id, "displayname") != settings.display_name:
            rpc.set_config(account_id, "displayname", settings.display_name)
        if args.command == "probe-vcard":
            row = database.connection.execute(
                "SELECT contact_id FROM users WHERE contact_id>0 ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                raise RuntimeError("数据库中没有可用于名片验证的联系人")
            vcard = rpc.make_vcard(account_id, [row["contact_id"]])
            if "BEGIN:VCARD" not in vcard or "END:VCARD" not in vcard:
                raise RuntimeError("Delta Chat Core 返回的联系人名片格式无效")
            print("VCARD_PROBE_OK")
            return 0
        service = BotService(database, settings.secret)
        DeltaAdapter(rpc, service, settings).run_forever(account_id)
    return 0


def _resolve_account(rpc: Rpc, configured_id: int | None) -> int:
    if configured_id is not None:
        return configured_id
    accounts = rpc.get_all_account_ids()
    return accounts[0] if accounts else rpc.add_account()


if __name__ == "__main__":
    sys.exit(main())
