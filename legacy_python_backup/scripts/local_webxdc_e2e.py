"""Real two-account Webxdc transport and persistence check.

Uses configured accounts in an existing Delta Chat accounts directory. It sends
one clearly labelled test Webxdc file and two status updates between the accounts.
No account configuration or existing messages are changed.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from deltachat2 import MessageData, Rpc
from deltachat2.transport import IOTransport


def wait_until(label, predicate, timeout=120):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - mail transport is eventually consistent
            last_error = exc
        time.sleep(2)
    suffix = f" ({type(last_error).__name__})" if last_error else ""
    raise TimeoutError(f"等待{label}超时{suffix}")


def updates(rpc, account_id, message_id):
    return json.loads(rpc.get_webxdc_status_updates(account_id, message_id, 0))


def find_message(rpc, account_id, marker):
    for chat_id in rpc.get_chatlist_entries(account_id, None, None, None):
        for message_id in reversed(rpc.get_message_ids(account_id, chat_id, False, False)):
            message = rpc.get_message(account_id, message_id)
            if marker in (message.text or ""):
                return message_id
    return None


def protected_pair(rpc, sender, receiver, sender_contact, receiver_contact):
    return (
        rpc.get_contact(sender, receiver_contact).is_verified
        and rpc.get_contact(receiver, sender_contact).is_verified
    )


def run(args):
    marker = f"Webxdc真实联调-{int(time.time())}"
    with IOTransport(rpc_executable=args.rpc_server, cwd=args.data_root) as transport:
        rpc = Rpc(transport)
        account_ids = rpc.get_all_account_ids()
        if args.sender not in account_ids or args.receiver not in account_ids:
            raise RuntimeError(f"账号不存在；当前账号 ID：{account_ids}")
        sender_addr = rpc.get_config(args.sender, "addr")
        receiver_addr = rpc.get_config(args.receiver, "addr")
        receiver_contact = rpc.lookup_contact_id_by_addr(args.sender, receiver_addr)
        sender_contact = rpc.lookup_contact_id_by_addr(args.receiver, sender_addr)
        if not receiver_contact:
            receiver_contact = rpc.create_contact(
                args.sender, receiver_addr, "Webxdc联调接收方"
            )
        if not sender_contact:
            sender_contact = rpc.create_contact(
                args.receiver, sender_addr, "Webxdc联调发送方"
            )
        sender_chat = rpc.create_chat_by_contact_id(args.sender, receiver_contact)
        receiver_chat = rpc.create_chat_by_contact_id(args.receiver, sender_contact)
        rpc.start_io(args.sender)
        rpc.start_io(args.receiver)
        try:
            if not rpc.get_basic_chat_info(args.sender, sender_chat).is_encrypted:
                raise RuntimeError("发送方当前没有接收方的加密密钥，请先在客户端建立加密聊天")
            sender_instance = rpc.send_msg(
                args.sender,
                sender_chat,
                MessageData(file=str(Path(args.xdc).resolve()), text=marker),
            )
            receiver_instance = wait_until(
                "接收 Webxdc 文件",
                lambda: find_message(rpc, args.receiver, marker),
            )
            receiver_chat = rpc.get_message(args.receiver, receiver_instance).chat_id
            wait_until(
                "接收方建立加密回程",
                lambda: rpc.get_basic_chat_info(args.receiver, receiver_chat).is_encrypted,
            )
            first = {"payload": {"type": "persistence-probe", "step": 1, "marker": marker}}
            rpc.send_webxdc_status_update(args.sender, sender_instance, json.dumps(first), "")
            wait_until(
                "接收第一条状态更新",
                lambda: any(u.get("payload", {}).get("marker") == marker for u in updates(rpc, args.receiver, receiver_instance)),
            )
            second = {"payload": {"type": "persistence-probe", "step": 2, "marker": marker}}
            rpc.send_webxdc_status_update(args.receiver, receiver_instance, json.dumps(second), "")
            wait_until(
                "接收回传状态更新",
                lambda: any(u.get("payload", {}).get("step") == 2 and u.get("payload", {}).get("marker") == marker for u in updates(rpc, args.sender, sender_instance)),
            )
        finally:
            rpc.stop_io(args.sender)
            rpc.stop_io(args.receiver)
    with IOTransport(rpc_executable=args.rpc_server, cwd=args.data_root) as transport:
        rpc = Rpc(transport)
        sender_saved = updates(rpc, args.sender, sender_instance)
        receiver_saved = updates(rpc, args.receiver, receiver_instance)
        assert any(u.get("payload", {}).get("step") == 2 for u in sender_saved)
        assert any(u.get("payload", {}).get("step") == 1 for u in receiver_saved)
    print("E2E PASS: Webxdc 文件在两个真实账号间送达")
    print("E2E PASS: 双向状态更新送达")
    print("E2E PASS: Core 重启后双方状态更新仍可读取")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc-server", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--xdc", required=True)
    parser.add_argument("--sender", type=int, default=4)
    parser.add_argument("--receiver", type=int, default=5)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
