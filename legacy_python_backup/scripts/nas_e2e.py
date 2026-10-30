from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from deltachat2 import MessageData, Rpc
from deltachat2.transport import IOTransport

TIMEOUT = int(os.environ.get("E2E_TIMEOUT_SECONDS", "120"))


def stage(name: str) -> None:
    print(f"E2E PASS: {name}", flush=True)


def wait_until(description: str, predicate, timeout: int = TIMEOUT):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - Core may be busy during SecureJoin.
            last_error = exc
        time.sleep(2)
    suffix = f" ({type(last_error).__name__})" if last_error else ""
    raise TimeoutError(f"timed out waiting for {description}{suffix}")


def main() -> int:
    rpc_server = os.environ.get("E2E_RPC_SERVER", "/app/rpc/deltachat-rpc-server")
    account_qr = os.environ.get("E2E_ACCOUNT_QR", "dcaccount:nine.testrun.org")
    account_dir = Path(os.environ.get("E2E_ACCOUNT_DIR", "/e2e/account"))
    invite_file = Path(os.environ.get("E2E_INVITE_FILE", "/e2e/account-link.txt"))
    network_code = os.environ.get("CONTACTBOT_INITIAL_NETWORK_CODE") or os.environ.get(
        "INITIAL_NETWORK_CODE", ""
    )
    if not network_code:
        raise RuntimeError("CONTACTBOT_INITIAL_NETWORK_CODE is unavailable")
    if not invite_file.is_file():
        raise RuntimeError("SecureJoin invitation is unavailable")

    account_dir.mkdir(parents=True, exist_ok=True)
    with IOTransport(rpc_executable=rpc_server, cwd=str(account_dir)) as transport:
        rpc = Rpc(transport)
        account_ids = rpc.get_all_account_ids()
        account_id = account_ids[0] if account_ids else rpc.add_account()
        if not rpc.is_configured(account_id):
            rpc.add_transport_from_qr(account_id, account_qr)
        wait_until("temporary account configuration", lambda: rpc.is_configured(account_id), 180)
        rpc.set_config(account_id, "displayname", "联系人网络联调账号")
        rpc.start_io(account_id)
        stage("temporary account configured")

        invite_text = invite_file.read_text(encoding="utf-8")
        invite_lines = [line.strip() for line in invite_text.splitlines() if line.strip()]
        invite = next((line for line in invite_lines if line.startswith("OPENPGP4FPR:")), "")
        if not invite:
            invitation_url = next(
                (line for line in invite_lines if line.startswith("https://i.delta.chat/")), ""
            )
            fragment = unquote(urlsplit(invitation_url).fragment)
            if fragment:
                invite = f"OPENPGP4FPR:{fragment}"
        if not invite:
            raise RuntimeError("SecureJoin invitation format is unsupported")
        chat_id = rpc.secure_join(account_id, invite)

        def protected_contact():
            chat = rpc.get_basic_chat_info(account_id, chat_id)
            contact_ids = rpc.get_chat_contacts(account_id, chat_id)
            if not contact_ids:
                return None
            contact = rpc.get_contact(account_id, contact_ids[0])
            return contact if chat.is_encrypted and contact.is_verified else None

        wait_until("mutually verified SecureJoin", protected_contact, 180)
        stage("SecureJoin completed and contact verified")

        known_ids = set(rpc.get_message_ids(account_id, chat_id, False, False))

        def send_and_wait(text: str, expected: str):
            nonlocal known_ids
            rpc.send_msg(account_id, chat_id, MessageData(text=text))

            def response():
                nonlocal known_ids
                current_ids = rpc.get_message_ids(account_id, chat_id, False, False)
                for msg_id in current_ids:
                    if msg_id in known_ids:
                        continue
                    message = rpc.get_message(account_id, msg_id)
                    known_ids.add(msg_id)
                    if not message.is_info and message.from_id != 1 and expected in message.text:
                        if not message.show_padlock:
                            raise RuntimeError("bot response was not encrypted")
                        return message.text
                return None

            return wait_until(f"response containing {expected}", response)

        send_and_wait("帮助", "常用指令")
        stage("encrypted private help command")

        join_reply = send_and_wait(f"加入 {network_code} 联调用户", "准备加入")
        token_match = re.search(r"确认\s+([A-Z0-9]+)", join_reply)
        if not token_match:
            raise RuntimeError("join confirmation token was not found")
        send_and_wait(f"确认 {token_match.group(1)}", "已加入")
        stage("join code, privacy notice, and confirmation")

        send_and_wait("我的资料", "成员编号")
        stage("member profile command")

        send_and_wait("管理", "没有管理员权限")
        stage("member/admin permission boundary")

        delete_reply = send_and_wait("删除我的数据", "这会删除")
        delete_match = re.search(r"确认\s+([A-Z0-9]+)", delete_reply)
        if not delete_match:
            raise RuntimeError("delete confirmation token was not found")
        send_and_wait(f"确认 {delete_match.group(1)}", "资料已删除")
        stage("personal data deletion")

        rpc.stop_io(account_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
