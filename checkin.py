#!/usr/bin/env python3
"""
Telegram 机器人自动签到脚本

基于 Telethon，以用户身份模拟向各机器人发送签到命令，
支持自动点击回复消息中的按钮。配合 GitHub Actions 可实现每日定时自动签到。

用法：
    python checkin.py
"""
import asyncio
import logging
import os
import re
import sys

# 必须先加载 .env，再导入依赖环境变量的模块
from dotenv import load_dotenv
load_dotenv()

import yaml
from rich.console import Console
from rich.markdown import Markdown
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from email_notify import is_email_enabled, send_email_notify
from image_solver import (
    download_image_as_base64,
    get_button_options,
    solve_image_captcha,
)
from proxy import get_proxy, proxy_info_str

# ---------------------------------------------------------------------------
# 日志配置（控制台彩色 + emoji + rich Markdown 渲染，文件纯文本去标记）
# ---------------------------------------------------------------------------
# 用于将 Markdown 渲染为 ANSI 转义码的 Console 实例
_ansi_console = Console(force_terminal=True, color_system="auto", soft_wrap=True)


def _markdown_to_ansi(text):
    """使用 rich 库将 Markdown 渲染为终端 ANSI 转义码"""
    with _ansi_console.capture() as capture:
        _ansi_console.print(Markdown(text), end="")
    return capture.get().rstrip("\n")


def _strip_markdown_log(text):
    """去除 Markdown 语法符号（用于纯文本日志文件）"""
    # 链接 [text](url) → text (url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    # 粗体 **text** → text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # 代码 `text` → text
    text = re.sub(r'`(.+?)`', r'\1', text)
    # 斜体 *text* → text
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    return text


class ColorFormatter(logging.Formatter):
    """给日志级别加 ANSI 颜色，同时用 rich 渲染消息中的 Markdown 语法"""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record):
        original_levelname = record.levelname
        original_msg = record.msg
        original_args = record.args

        color = self.COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        # 只对消息部分渲染 Markdown（不影响时间戳和级别）
        record.msg = _markdown_to_ansi(record.getMessage())
        record.args = None

        formatted = super().format(record)

        # 恢复原始值，避免影响文件处理器
        record.levelname = original_levelname
        record.msg = original_msg
        record.args = original_args

        return formatted


class StripMarkdownFormatter(logging.Formatter):
    """去除消息中的 Markdown 语法符号（用于纯文本日志文件）"""

    def format(self, record):
        formatted = super().format(record)
        return _strip_markdown_log(formatted)


_log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(ColorFormatter(_log_fmt))
_file = logging.FileHandler("checkin.log", encoding="utf-8")
_file.setFormatter(StripMarkdownFormatter(_log_fmt))
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logger = logging.getLogger("tg-checkin")

# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
CONFIG_FILE = os.getenv("CONFIG_FILE", "config.yaml")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "")
NOTIFY_MAX_LENGTH = int(os.getenv("NOTIFY_MAX_LENGTH", "50") or "50")


def load_bots(path):
    """从 YAML 配置文件读取机器人列表"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg or "bots" not in cfg:
        raise ValueError("配置文件格式错误：缺少 bots 字段")
    return cfg["bots"]


# ---------------------------------------------------------------------------
# 核心签到逻辑
# ---------------------------------------------------------------------------
async def wait_response(client, entity, timeout=30):
    """等待来自指定机器人的下一条消息（新消息或编辑消息），超时返回 None

    很多机器人在按钮点击后会「编辑」原消息而不是发新消息，
    所以同时监听 NewMessage 和 MessageEdited 两种事件。
    """
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def handler(event):
        if not future.done():
            future.set_result(event.message)

    new_msg = events.NewMessage(from_users=entity)
    edited_msg = events.MessageEdited(from_users=entity)
    client.add_event_handler(handler, new_msg)
    client.add_event_handler(handler, edited_msg)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        client.remove_event_handler(handler)


async def wait_reply_to(client, entity, reply_to_id, timeout=10):
    """等待引用指定消息的回复消息

    只捕获 reply_to_msg_id 等于 reply_to_id 的新消息或编辑消息，
    忽略其他消息（如"请稍后"等过渡消息）。
    超时返回 None。
    """
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def handler(event):
        msg = event.message
        reply_to = getattr(msg, "reply_to", None)
        if reply_to and getattr(reply_to, "reply_to_msg_id", None) == reply_to_id:
            if not future.done():
                future.set_result(msg)

    new_msg = events.NewMessage(from_users=entity)
    edited_msg = events.MessageEdited(from_users=entity)
    client.add_event_handler(handler, new_msg)
    client.add_event_handler(handler, edited_msg)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        client.remove_event_handler(handler)


def log_buttons(name, message):
    """输出消息中的所有按钮文字，方便确认 click_button 配置"""
    if not message.buttons:
        return
    all_btns = []
    for row in message.buttons:
        for btn in row:
            btn_text = getattr(btn, "text", "") or ""
            if btn_text:
                all_btns.append(btn_text)
    if all_btns:
        logger.info("\U0001f3f2\ufe0f [%s] 可用按钮: %s", name, " | ".join(all_btns))


async def click_button(message, text):
    """点击消息中包含指定文字的 inline 按钮，成功返回 True"""
    if not message.buttons:
        return False
    for row in message.buttons:
        for btn in row:
            btn_text = getattr(btn, "text", "") or ""
            if text in btn_text:
                await btn.click()
                return True
    return False


async def click_answer_button(message, answer):
    """点击消息中与答案匹配的按钮，成功返回 True

    按钮文字可能是纯数字，也可能包含数字。
    按优先级依次尝试：精确匹配 → 数值匹配 → 包含匹配。
    """
    if not message.buttons:
        return False

    answer_str = str(answer)
    # 处理浮点数：如果答案是 3.0，按钮可能是 "3"
    if isinstance(answer, float) and answer == int(answer):
        answer_str = str(int(answer))

    # 第一轮：精确匹配按钮文字
    for row in message.buttons:
        for btn in row:
            btn_text = (getattr(btn, "text", "") or "").strip()
            if btn_text == answer_str:
                await btn.click()
                return True

    # 第二轮：按钮文字是纯数字且数值等于答案
    for row in message.buttons:
        for btn in row:
            btn_text = (getattr(btn, "text", "") or "").strip()
            try:
                if float(btn_text) == float(answer):
                    await btn.click()
                    return True
            except (ValueError, TypeError):
                continue

    # 第三轮：按钮文字包含答案数字
    for row in message.buttons:
        for btn in row:
            btn_text = (getattr(btn, "text", "") or "").strip()
            if answer_str in btn_text:
                await btn.click()
                return True

    return False


def solve_math_question(text):
    """从消息文本中提取数学算式并计算结果

    支持的运算符：+ - × * ÷ /（含全角和 emoji 变体）
    例如："3 + 5 = ?" → (8, "3 + 5"), "12 ÷ 4 = ?" → (3.0, "12 / 4")

    返回 (result, expression_str) 或 (None, None) 如果未找到算式
    """
    if not text:
        return None, None

    # 统一各种运算符号为标准 ASCII
    normalized = (
        text.replace("\u00d7", "*")   # ×
            .replace("\u00f7", "/")   # ÷
            .replace("\u2715", "*")   # ✕
            .replace("\u2716", "*")   # ✖
            .replace("\uff0b", "+")   # ＋ (全角)
            .replace("\uff0d", "-")   # － (全角)
            .replace("\uff0a", "*")   # ＊ (全角)
            .replace("\uff0f", "/")   # ／ (全角)
            .replace("\u2795", "+")   # ➕ (emoji)
            .replace("\u2796", "-")   # ➖ (emoji)
            .replace("\u2797", "/")   # ➗ (emoji)
    )

    # 匹配算式：数字 运算符 数字（后面可能有 = ? 等）
    pattern = r"(\d+(?:\.\d+)?)\s*([\+\-\*/])\s*(\d+(?:\.\d+)?)"
    match = re.search(pattern, normalized)
    if not match:
        return None, None

    a_str, op, b_str = match.group(1), match.group(2), match.group(3)
    a, b = float(a_str), float(b_str)
    expr_str = f"{a_str} {op} {b_str}"

    if op == "+":
        result = a + b
    elif op == "-":
        result = a - b
    elif op == "*":
        result = a * b
    elif op == "/":
        if b == 0:
            return None, None
        result = a / b
    else:
        return None, None

    # 如果结果是整数，返回 int 方便后续匹配按钮
    if result == int(result):
        result = int(result)

    return result, expr_str


async def get_updated_message(client, entity, original_msg, timeout=15):
    """获取操作后的机器人回复

    很多机器人在按钮点击后会「编辑」原消息而不是发新消息，
    所以依次尝试：重新拉取原消息（检查是否被编辑）→ 拉取最近消息 → 等待新消息事件。
    返回最新的消息对象，失败返回 None。
    """
    original_text = original_msg.text or ""

    # 方法1：重新拉取原消息，检查是否被编辑
    try:
        updated = await client.get_messages(entity, ids=original_msg.id)
        if updated and (updated.text or updated.buttons):
            if updated.text != original_text:
                return updated
    except Exception:  # noqa: BLE001
        pass

    await asyncio.sleep(1)

    # 方法2：拉取聊天中最近的消息（机器人可能发了新消息）
    try:
        recent = await client.get_messages(entity, limit=3)
        for msg in recent:
            if (msg and msg.text and msg.id != original_msg.id
                    and not msg.out):  # 排除用户自己发的消息
                return msg
    except Exception:  # noqa: BLE001
        pass

    # 方法3：等待新消息或编辑事件
    return await wait_response(client, entity, timeout)


def _summarize_message(message, max_length=50):
    """从长消息中提取关键内容用于通知

    保留从开头开始的非空行，直到总长度接近 max_length。
    如果消息本身不超过 max_length，则原样返回。
    """
    if len(message) <= max_length:
        return message

    lines = message.split("\n")
    summary_lines = []
    current_length = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if current_length + len(stripped) > max_length and summary_lines:
            break
        summary_lines.append(stripped)
        current_length += len(stripped) + 1  # +1 for newline

    if not summary_lines:
        return message[:max_length] + " ..."

    result = "\n".join(summary_lines)
    if len(result) < len(message.rstrip()):
        result += " ..."
    return result


async def do_checkin(client, bot_cfg):
    """对单个机器人执行签到，返回结果字典"""
    name = bot_cfg.get("name", bot_cfg["username"])
    username = bot_cfg["username"]
    command = bot_cfg.get("command", "/checkin")
    click_text = bot_cfg.get("click_button")
    solve_math = bot_cfg.get("solve_math", False)
    solve_image = bot_cfg.get("solve_image", False)
    ai_provider = bot_cfg.get("ai_provider")
    ai_model = bot_cfg.get("ai_model")
    timeout = bot_cfg.get("timeout", 30)
    delay = bot_cfg.get("delay", 2)
    debug = bot_cfg.get("debug", False)
    wait_final = bot_cfg.get("wait_final", False)

    result = {"name": name, "username": username, "success": False, "message": ""}

    # 1. 获取机器人实体
    try:
        entity = await client.get_entity(username)
    except Exception as e:  # noqa: BLE001
        result["message"] = f"获取机器人失败: {e}"
        logger.error("\u274c [%s] %s", name, result["message"])
        return result

    try:
        # 2. 发送签到命令
        await asyncio.sleep(delay)
        sent = await client.send_message(entity, command)
        logger.info("\U0001f4e4 [%s] 已发送命令: %s", name, command)

        # 3. 等待机器人回复
        response = await wait_response(client, entity, timeout)
        if response:
            reply_text = response.text or ""
            result["message"] = reply_text
            logger.info("\U0001f4e5 [%s] 机器人回复: %s", name, reply_text)

            # 4. 可选：点击回复消息中的按钮
            if click_text:
                if debug:
                    log_buttons(name, response)

                clicked = await click_button(response, click_text)
                if not clicked:
                    logger.warning("\u26a0\ufe0f [%s] 未找到匹配按钮: %s", name, click_text)
                else:
                    logger.info("\U0001f5b1\ufe0f [%s] 已点击按钮: %s", name, click_text)
                    await asyncio.sleep(2)

                    # 获取点击按钮后的回复（可能是编辑原消息或新消息）
                    response = await get_updated_message(client, entity, response, timeout)
                    if response:
                        reply_text = response.text or ""
                        result["message"] = reply_text
                        logger.info("\U0001f4e5 [%s] 按钮回复: %s", name, reply_text)

            # 5. 可选：解答数学验证题
            if solve_math and response:
                if debug:
                    log_buttons(name, response)

                answer, expr = solve_math_question(reply_text)
                if answer is None:
                    # 未识别到算式，可能机器人本次未出数学题，继续后续步骤
                    logger.warning("\u26a0\ufe0f [%s] 未识别到数学算式: %s", name, reply_text)
                else:
                    logger.info("\U0001f9ee [%s] 识别算式: %s = %s", name, expr, answer)

                    clicked = await click_answer_button(response, answer)
                    if not clicked:
                        logger.warning("\u26a0\ufe0f [%s] 未找到答案按钮: %s", name, answer)
                        result["message"] = f"未找到答案按钮({answer})"
                        return result

                    logger.info("\U0001f5b1\ufe0f [%s] 已选择答案: %s", name, answer)
                    await asyncio.sleep(2)

                    # 获取选择答案后的最终回复
                    response = await get_updated_message(client, entity, response, timeout)
                    if response:
                        reply_text = response.text or ""
                        result["message"] = reply_text
                        logger.info("\U0001f4e5 [%s] 签到结果: %s", name, reply_text)

            # 6. 可选：图片验证码识别
            if solve_image and response:
                has_image = response.photo or (
                    response.document
                    and (response.document.mime_type or "").startswith("image/")
                )
                if debug:
                    logger.info(
                        "[checkin] [%s] solve_image 检查: has_image=%s, "
                        "photo=%s, document=%s",
                        name, has_image,
                        bool(response.photo),
                        bool(response.document),
                    )
                if has_image:
                    if debug:
                        log_buttons(name, response)

                    options = get_button_options(response)
                    if debug:
                        logger.info(
                            "[checkin] [%s] 提取到 %d 个按钮选项: %s",
                            name, len(options), options,
                        )

                    if not options:
                        logger.warning("\u26a0\ufe0f [%s] 图片验证码未找到选项按钮", name)
                        result["message"] = "图片验证码未找到选项按钮"
                        return result

                    logger.info("\U0001f4f7 [%s] 检测到图片验证码，选项: %s", name, " | ".join(options))

                    image_b64, mime_type = await download_image_as_base64(
                        client, response, debug=debug
                    )
                    if not image_b64:
                        logger.warning("\u26a0\ufe0f [%s] 图片下载失败", name)
                        result["message"] = "图片验证码下载失败"
                        return result

                    if debug:
                        logger.info(
                            "[checkin] [%s] 图片已下载: mime_type=%s, "
                            "base64_length=%d",
                            name, mime_type, len(image_b64),
                        )
                        logger.info(
                            "[checkin] [%s] 调用 AI: provider=%s, model=%s",
                            name, ai_provider or "auto", ai_model or "default",
                        )

                    answer = await solve_image_captcha(
                        image_b64, mime_type, options,
                        provider=ai_provider, model=ai_model,
                        debug=debug,
                    )
                    if not answer:
                        logger.warning("\u26a0\ufe0f [%s] 图片识别失败", name)
                        result["message"] = "图片验证码识别失败"
                        return result

                    logger.info("\U0001f916 [%s] AI 识别答案: %s", name, answer)

                    if debug:
                        logger.info(
                            "[checkin] [%s] 尝试点击按钮，匹配文字: '%s'",
                            name, answer,
                        )

                    clicked = await click_button(response, answer)
                    if not clicked:
                        # 尝试用 click_answer_button 做更灵活的匹配
                        if debug:
                            logger.info(
                                "[checkin] [%s] 精确匹配失败，尝试模糊匹配",
                                name,
                            )
                        clicked = await click_answer_button(response, answer)

                    if debug and clicked:
                        logger.info("[checkin] [%s] 按钮点击成功", name)

                    if not clicked:
                        logger.warning("\u26a0\ufe0f [%s] 未找到答案按钮: %s", name, answer)
                        result["message"] = f"未找到答案按钮({answer})"
                        return result

                    logger.info("\U0001f5b1\ufe0f [%s] 已选择答案: %s", name, answer)
                    await asyncio.sleep(2)

                    # 获取选择答案后的最终回复
                    final_resp = await get_updated_message(client, entity, response, timeout)
                    if final_resp:
                        final_text = final_resp.text or ""
                        result["message"] = final_text
                        logger.info("\U0001f4e5 [%s] 签到结果: %s", name, final_text)
                        if debug:
                            logger.info(
                                "[checkin] [%s] 最终回复完整内容:\n%s",
                                name, final_text,
                            )

            # 7. 等待引用签到命令的回复消息
            #    部分机器人会先发"请稍后"等过渡消息（非引用），
            #    再发一条引用签到命令的回复消息，包含实际签到结果
            if response:
                reply_to = getattr(response, "reply_to", None)
                is_command_reply = (
                    reply_to
                    and getattr(reply_to, "reply_to_msg_id", None) == sent.id
                )
                if not is_command_reply:
                    logger.info("\u23f3 [%s] 等待签到结果回复...", name)
                    reply = await wait_reply_to(
                        client, entity, sent.id, timeout=min(timeout, 10)
                    )
                    if reply and reply.text:
                        result["message"] = reply.text
                        logger.info("\U0001f4e5 [%s] 签到结果: %s", name, reply.text)
                    elif wait_final:
                        # wait_final 备选：等待任意第二条消息
                        logger.info("\u23f3 [%s] 等待最终回复...", name)
                        final = await wait_response(client, entity, timeout=timeout)
                        if final and final.text:
                            result["message"] = final.text
                            logger.info("\U0001f4e5 [%s] 签到结果: %s", name, final.text)

            result["success"] = True
        else:
            # 命令已发出但机器人没回复，通常也算签到成功
            result["message"] = "机器人未回复（命令已发送）"
            result["success"] = True

    except FloodWaitError as e:
        result["message"] = f"触发限流，需等待 {e.seconds} 秒"
        logger.error("\u274c [%s] %s", name, result["message"])
    except Exception as e:  # noqa: BLE001
        result["message"] = f"签到异常: {e}"
        logger.error("\u274c [%s] %s", name, result["message"])

    # 超过阈值的回复，精简通知内容（邮件/Telegram 通知只发送关键行）
    if result["message"] and len(result["message"]) > NOTIFY_MAX_LENGTH:
        result["notify_message"] = _summarize_message(
            result["message"], NOTIFY_MAX_LENGTH
        )

    return result


async def send_notify(client, chat_id, text):
    """把签到结果通过 Telegram 发给自己"""
    try:
        await client.send_message(chat_id, text)
        logger.info("\U0001f514 结果已发送至 %s", chat_id)
    except Exception as e:  # noqa: BLE001
        logger.error("\u274c 发送通知失败: %s", e)


async def main():
    # ---- 参数校验 ----
    if not API_ID or not API_HASH:
        logger.error("\u274c 请在 .env 中配置 API_ID 和 API_HASH")
        sys.exit(1)
    if not SESSION_STRING:
        logger.error("\u274c 请先运行 python login.py 获取 SESSION_STRING，并填入 .env")
        sys.exit(1)

    bots = load_bots(CONFIG_FILE)
    logger.info("\U0001f4cb 共配置 %d 个机器人", len(bots))

    # ---- 创建客户端并验证登录 ----
    proxy = get_proxy()
    logger.info("\U0001f310 连接方式: %s", proxy_info_str())
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, proxy=proxy)
    await client.connect()

    if not await client.is_user_authorized():
        logger.error("\u274c SESSION_STRING 无效或已过期，请重新运行 login.py 获取")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    logger.info("\U0001f464\u200d\U0001f4bb 已登录账号: %s (@%s)", me.first_name, me.username or "无")

    # ---- 依次签到 ----
    results = []
    for bot_cfg in bots:
        logger.info("\u2500" * 40)
        result = await do_checkin(client, bot_cfg)
        results.append(result)

    # ---- 结果汇总 ----
    success_count = sum(1 for r in results if r["success"])
    logger.info("\u2550" * 50)
    logger.info("\U0001f4ca 签到结果汇总")
    for r in results:
        mark = "\u2705 成功" if r["success"] else "\u274c 失败"
        logger.info(
            "  %s %s (@%s): %s",
            mark, r["name"], r["username"], r["message"],
        )
    logger.info("\U0001f4ca 总计: \u2705 %d / %d", success_count, len(results))

    # ---- 可选：发送 Telegram 通知 ----
    if NOTIFY_CHAT_ID:
        lines = [f"TG 签到结果 {success_count}/{len(results)}"]
        for r in results:
            mark = "OK" if r["success"] else "X"
            lines.append(f"[{mark}] {r['name']}: {r.get('notify_message') or r['message']}")
        await send_notify(client, NOTIFY_CHAT_ID, "\n".join(lines))

    await client.disconnect()

    # ---- 可选：发送邮件通知 ----
    if is_email_enabled():
        await send_email_notify(results)

    # 有失败时退出码非 0，方便 CI 识别
    if success_count < len(results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
