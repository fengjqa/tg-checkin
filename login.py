#!/usr/bin/env python3
"""
首次登录工具：生成 SESSION_STRING

提供两种登录方式：
  1. 扫码登录（推荐）：用已登录的 Telegram App 扫描二维码，无需验证码
     → 适合国内手机号收不到短信验证码的情况
  2. 手机号 + 验证码：验证码优先发到 Telegram App 内，非短信

用法：
    python login.py          # 交互式选择登录方式
    python login.py --qr     # 直接扫码登录
    python login.py --phone  # 直接手机号登录
"""
import asyncio
import base64
import os
import sys
import time

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.auth import ExportLoginTokenRequest
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.types.auth import (
    LoginToken,
    LoginTokenMigrateTo,
    LoginTokenSuccess,
)
from dotenv import load_dotenv

from proxy import get_proxy, proxy_info_str

load_dotenv()

# Telegram 数据中心地址表（DC 迁移时使用）
# https://core.telegram.org/datacenter
TELEGRAM_DC = {
    1: ("149.154.167.50", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.167.80", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


def print_qr(url):
    """在终端打印二维码，未安装 qrcode 库时回退为打印链接"""
    try:
        import qrcode
    except ImportError:
        print("未安装 qrcode 库，请运行: pip install qrcode")
        print("或将以下链接在 Telegram 中打开：")
        print(url)
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    print()
    qr.print_ascii(invert=True)
    print()


def clear_screen():
    """清屏"""
    os.system("cls" if os.name == "nt" else "clear")


def write_session_to_env(env_path, key, value):
    """将 SESSION_STRING 自动写入 .env 文件

    如果 .env 已存在，更新对应行；不存在则新建。
    """
    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)

    if not found:
        lines.append(f"\n{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def handle_2fa(client):
    """处理两步验证：提示用户输入云密码并完成登录"""
    import getpass

    print()
    print("=" * 60)
    print("检测到账号开启了两步验证，请输入云密码")
    print()
    print("这是你在 Telegram 设置中设置的「两步验证密码」")
    print("（不是短信验证码，也不是登录密码）")
    print("=" * 60)
    password = getpass.getpass("请输入两步验证密码: ")
    try:
        await client.sign_in(password=password)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"密码验证失败: {e}")
        return False


async def fetch_dc_map(client):
    """从 Telegram 服务器获取所有 DC 的最新地址

    连接到 DC 2 后调用 help.GetConfig，返回 {dc_id: (ip, port)} 字典。
    这比硬编码地址更可靠，因为 Telegram 会随时更新 DC 地址。
    """
    dc_map = {}
    try:
        config = await client(GetConfigRequest())
        for opt in config.dc_options:
            if opt.ipv6:
                continue
            # 优先使用普通地址（非 cdn、非 media_only）
            is_preferred = not getattr(opt, "cdn", False) and not getattr(
                opt, "media_only", False
            )
            if opt.id not in dc_map or is_preferred:
                dc_map[opt.id] = (opt.ip_address, opt.port)
        print(f"已从服务器获取 {len(dc_map)} 个 DC 地址")
    except Exception as e:  # noqa: BLE001
        print(f"获取 DC 地址失败，将使用备用地址: {e}")
    return dc_map


async def create_client_on_dc(dc_id, api_id, api_hash, proxy, dc_map=None):
    """在指定 DC 上创建并连接一个全新的客户端

    优先使用从服务器获取的 dc_map 地址，其次用硬编码的 TELEGRAM_DC。
    连接失败时自动重试 3 次。
    """
    if dc_map and dc_id in dc_map:
        server, port = dc_map[dc_id]
    else:
        server, port = TELEGRAM_DC.get(dc_id, ("149.154.167.51", 443))

    for attempt in range(3):
        try:
            session = StringSession()
            session.set_dc(dc_id, server, port)
            client = TelegramClient(session, api_id, api_hash, proxy=proxy)
            await client.connect()
            return client
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                print(f"连接 DC {dc_id} 失败 (尝试 {attempt + 1}/3): {e}")
                await asyncio.sleep(2)
            else:
                raise


async def qr_login(client, api_id, api_hash, proxy):
    """扫码登录流程

    通过 Telegram 的 QR 登录 API 生成二维码，
    用户用已登录的 App 扫码后即可完成授权，无需短信验证码。

    返回 (success, client)，client 可能在 DC 迁移后变为新实例。
    """
    # 先从服务器获取所有 DC 的最新地址（比硬编码更可靠）
    print("正在获取服务器信息...")
    dc_map = await fetch_dc_map(client)

    clear_screen()
    print("=" * 60)
    print("扫码登录")
    print()
    print("请打开手机 Telegram App：")
    print("  iOS:   设置 → 设备 → 扫描二维码")
    print("  安卓:  设置 → 设备 → 扫描二维码")
    print("  (英文: Settings → Devices → Scan QR Code)")
    print("=" * 60)

    while True:
        # 1. 请求登录令牌
        try:
            result = await client(ExportLoginTokenRequest(
                api_id=api_id,
                api_hash=api_hash,
                except_ids=[],
            ))
        except SessionPasswordNeededError:
            if await handle_2fa(client):
                return True, client
            return False, client
        except Exception as e:  # noqa: BLE001
            print(f"获取登录令牌失败: {e}")
            return False, client

        # 2. 处理 DC 迁移（用户账号在其他 DC 时需要切换）
        #    创建全新客户端连接到目标 DC，避免内部状态残留
        if isinstance(result, LoginTokenMigrateTo):
            dc_id = result.dc_id
            addr = dc_map.get(dc_id) or TELEGRAM_DC.get(dc_id, ("149.154.167.51", 443))
            print(f"正在切换到 DC {dc_id} ({addr[0]}:{addr[1]})...")
            await client.disconnect()
            await asyncio.sleep(1)
            client = await create_client_on_dc(dc_id, api_id, api_hash, proxy, dc_map)
            continue

        # 3. 已成功（首次调用不太可能，以防万一）
        if isinstance(result, LoginTokenSuccess):
            print("\n登录成功！")
            return True, client

        # 4. 显示二维码并轮询等待扫描
        if isinstance(result, LoginToken):
            token = result.token
            url = f"tg://login?token={base64.urlsafe_b64encode(token).decode()}"

            clear_screen()
            print("=" * 60)
            print("请使用 Telegram App 扫描以下二维码：")
            print("  设置 → 设备 → 扫描二维码")
            print("=" * 60)
            print_qr(url)
            print("等待扫描确认...（二维码 30 秒后自动刷新）")
            print()

            # 轮询，最多等 30 秒后刷新二维码
            poll_start = time.time()
            while time.time() - poll_start < 30:
                await asyncio.sleep(1.5)
                try:
                    result = await client(ExportLoginTokenRequest(
                        api_id=api_id,
                        api_hash=api_hash,
                        except_ids=[],
                    ))
                except SessionPasswordNeededError:
                    if await handle_2fa(client):
                        clear_screen()
                        print("=" * 60)
                        print("登录成功！")
                        print("=" * 60)
                        return True, client
                    return False, client
                except Exception:  # noqa: BLE001
                    continue

                if isinstance(result, LoginTokenSuccess):
                    clear_screen()
                    print("=" * 60)
                    print("扫码登录成功！")
                    print("=" * 60)
                    return True, client

                if isinstance(result, LoginTokenMigrateTo):
                    dc_id = result.dc_id
                    addr = dc_map.get(dc_id) or TELEGRAM_DC.get(dc_id, ("149.154.167.51", 443))
                    print(f"正在切换到 DC {dc_id} ({addr[0]}:{addr[1]})...")
                    await client.disconnect()
                    await asyncio.sleep(1)
                    client = await create_client_on_dc(dc_id, api_id, api_hash, proxy, dc_map)
                    break  # 跳出内层循环，重新生成二维码

                # token 变了，需要刷新二维码
                if isinstance(result, LoginToken) and result.token != token:
                    break
            else:
                print("二维码已过期，正在重新生成...")
                continue


async def phone_login(client, phone):
    """手机号 + 验证码登录

    验证码会优先发送到已登录的 Telegram App 内（以消息形式），
    只有在没有其他设备在线时才会通过短信发送。
    """
    print("=" * 60)
    print("手机号登录")
    print()
    print("提示：验证码会优先发送到你的 Telegram App（已登录的设备）")
    print("如果手机上已有 Telegram 登录，请在 App 内查看验证码消息")
    print("只有在没有其他设备在线时，才会通过短信发送验证码")
    print("=" * 60)

    await client.start(phone=phone)
    return True


async def main():
    api_id = int(os.getenv("API_ID", "0") or "0")
    api_hash = os.getenv("API_HASH", "")
    phone = os.getenv("PHONE", "")

    if not api_id or not api_hash:
        print("错误：请先在 .env 中配置 API_ID 和 API_HASH")
        print("获取地址：https://my.telegram.org -> API development tools")
        return

    # 解析命令行参数
    method = None
    if "--qr" in sys.argv:
        method = "qr"
    elif "--phone" in sys.argv:
        method = "phone"

    # 交互式选择
    if method is None:
        print("=" * 60)
        print("请选择登录方式：")
        print()
        print("  1. 扫码登录（推荐）")
        print("     用已登录的 Telegram App 扫描二维码，无需验证码")
        print("     → 适合国内手机号收不到短信的情况")
        print()
        print("  2. 手机号 + 验证码")
        print("     验证码优先发到 Telegram App，非短信")
        print("=" * 60)
        choice = input("请输入 1 或 2: ").strip()
        method = "qr" if choice == "1" else "phone"

    proxy = get_proxy()
    print(f"连接方式: {proxy_info_str()}")

    # 创建并连接客户端（带重试）
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy)
    connected = False
    for attempt in range(3):
        try:
            await client.connect()
            connected = True
            break
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                print(f"连接 Telegram 失败 (尝试 {attempt + 1}/3): {e}")
                await asyncio.sleep(3)
            else:
                print()
                print("=" * 60)
                print("无法连接到 Telegram，请检查：")
                print()
                print("  1. 代理是否正常运行（端口 11223 是否在监听）")
                print("  2. 代理节点是否能访问 Telegram（换个节点试试）")
                print("  3. 代理端口是否正确（检查 .env 中的 PROXY_PORT）")
                print()
                print(f"  当前代理: {proxy_info_str()}")
                print(f"  错误信息: {e}")
                print("=" * 60)
                return

    if method == "qr":
        success, client = await qr_login(client, api_id, api_hash, proxy)
    else:
        if not phone:
            phone = input("请输入手机号（含国家代码，如 +8613800138000）: ").strip()
        success = await phone_login(client, phone)

    if not success:
        print("登录失败")
        await client.disconnect()
        return

    # 验证登录状态
    if not await client.is_user_authorized():
        print("登录失败：未获得授权")
        await client.disconnect()
        return

    session_string = client.session.save()
    me = await client.get_me()

    # 自动写入 .env 文件
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    write_session_to_env(env_path, "SESSION_STRING", session_string)

    print()
    print("=" * 60)
    print(f"登录成功！账号: {me.first_name} (@{me.username or '无'})")
    print(f"SESSION_STRING 已自动写入 .env 文件")
    print("=" * 60)
    print()
    print("提示：.env 文件包含你的登录凭证，请妥善保管，切勿泄露或提交到公开仓库。")
    print("现在可以直接运行: python checkin.py")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
