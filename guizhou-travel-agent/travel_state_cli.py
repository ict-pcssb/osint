"""Optional local console for explicit M3 account and draft actions."""
from dataclasses import asdict, is_dataclass
from getpass import getpass
import json

from travel_state_app import AuthRequired, Conflict, NotFound, TravelApplication


async def run_console(path, *, create_account=False):
    from travel_agent import Conversation

    application = TravelApplication(path)
    username = input("本地账号 > ").strip()
    password = getpass("密码 > ")
    if create_account:
        application.create_account(username, password)
        print("本地账号已创建。")
        return
    token = application.login(username, password)
    del password
    agent = Conversation(typed_output=True)
    print("输入消息，或提交 JSON 操作 preview、save、read。回车退出。")
    try:
        while raw := input("游客 > ").strip():
            try:
                if raw.startswith("{"):
                    result = dispatch(application, token, json.loads(raw))
                else:
                    result = await application.turn(token, raw, conversation_factory=lambda: agent)
                print(json.dumps(asdict(result) if is_dataclass(result) else result, ensure_ascii=False))
            except (AuthRequired, NotFound, Conflict, TypeError, ValueError) as exc:
                print(str(exc))
    finally:
        application.logout(token)


def dispatch(application, token, payload):
    if not isinstance(payload, dict):
        raise ValueError("操作需为 JSON 对象")
    action = payload.pop("action", None)
    if action == "preview":
        return application.preview_trip(token, **payload)
    if action == "save":
        return application.create_trip(token, **payload)
    if action == "read":
        return application.read_trip(token, **payload)
    raise ValueError("支持的操作为 preview、save、read")

