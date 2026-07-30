import csv
import json
import os
import sys
from pathlib import Path

import requests


BASE_URL = os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8180")
ADMIN_USER = os.getenv("COMFYUI_ADMIN_USER", "")
ADMIN_PASS = os.getenv("COMFYUI_ADMIN_PASS", "")
CSV_PATH = Path(os.getenv("COMFYUI_USERS_CSV", "/mnt/Comfyui-admin/users.csv"))
OUTPUT_PATH = Path(
    os.getenv(
        "COMFYUI_REGISTER_RESULTS",
        "/mnt/Comfyui-admin/register_results.csv",
    )
)
TIMEOUT = float(os.getenv("COMFYUI_REGISTER_TIMEOUT", "15"))
VERIFY_SSL = os.getenv("COMFYUI_VERIFY_SSL", "true").lower() not in {
    "0",
    "false",
    "no",
}


def load_users_from_csv(csv_path: Path) -> list[dict[str, str]]:
    users = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"username", "password"}
        if not reader.fieldnames:
            raise ValueError("CSV 为空，或者没有表头。")
        if not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"CSV 表头必须包含: {sorted(required)}，当前是: {reader.fieldnames}"
            )
        for index, row in enumerate(reader, start=2):
            username = (row.get("username") or "").strip()
            password = (row.get("password") or "").strip()
            if not username or not password:
                print(f"[跳过] 第 {index} 行缺少 username 或 password")
                continue
            users.append({"username": username, "password": password})
    if not users:
        raise ValueError("没有读取到有效用户。")
    return users


def register_user(new_username: str, new_password: str) -> dict:
    payload = {
        "new_user_username": new_username,
        "new_user_password": new_password,
        "username": ADMIN_USER,
        "password": ADMIN_PASS,
    }
    try:
        response = requests.post(
            BASE_URL.rstrip("/") + "/register",
            data=payload,
            timeout=TIMEOUT,
            verify=VERIFY_SSL,
        )
    except requests.RequestException as error:
        return {
            "ok": False,
            "status_code": None,
            "message": f"请求异常: {error}",
        }

    response_text = response.text.strip()
    response_data = None
    if "application/json" in response.headers.get("Content-Type", "").lower():
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            pass

    message = ""
    if isinstance(response_data, dict):
        message = (
            str(response_data.get("message", "")).strip()
            or str(response_data.get("detail", "")).strip()
            or str(response_data.get("error", "")).strip()
        )
    return {
        "ok": 200 <= response.status_code < 300,
        "status_code": response.status_code,
        "message": message or response_text or f"HTTP {response.status_code}",
    }


def save_results(results: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["username", "success", "status_code", "message"],
        )
        writer.writeheader()
        writer.writerows(results)


def main() -> int:
    if not ADMIN_USER or not ADMIN_PASS:
        print(
            "错误: 请设置 COMFYUI_ADMIN_USER 和 COMFYUI_ADMIN_PASS 环境变量。",
            file=sys.stderr,
        )
        return 2
    if not CSV_PATH.exists():
        print(f"错误: CSV 文件不存在: {CSV_PATH.resolve()}", file=sys.stderr)
        return 2

    try:
        users = load_users_from_csv(CSV_PATH)
    except Exception as error:
        print(f"读取 CSV 失败: {error}", file=sys.stderr)
        return 2

    results = []
    success_count = 0
    print(f"准备注册 {len(users)} 个账号到 {BASE_URL}")
    for index, user in enumerate(users, start=1):
        username = user["username"]
        print(f"[{index}/{len(users)}] 正在创建用户: {username}")
        result = register_user(username, user["password"])
        success_count += int(result["ok"])
        print(
            f"  -> {'成功' if result['ok'] else '失败'}: "
            f"HTTP={result['status_code']} | {result['message']}"
        )
        results.append(
            {
                "username": username,
                "success": "YES" if result["ok"] else "NO",
                "status_code": result["status_code"]
                if result["status_code"] is not None
                else "",
                "message": result["message"],
            }
        )

    save_results(results, OUTPUT_PATH)
    failed = len(users) - success_count
    print(f"完成。成功 {success_count} 个，失败 {failed} 个")
    print(f"结果已保存到: {OUTPUT_PATH.resolve()}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
