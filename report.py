#!/usr/bin/env python3
"""
签到报告模块（周报 / 月报）

从 checkin_history.json 中读取历史签到记录，
汇总生成周报和月报，并通过邮件和/或 Telegram 发送。

历史记录格式（checkin_history.json）：
    [
      {
        "date": "2026-07-30",
        "timestamp": "2026-07-30T09:25:57",
        "bots": [
          {"name": "影巢", "username": "HDHiveBot", "success": true, "message": "..."},
          ...
        ],
        "success_count": 5,
        "total": 5
      },
      ...
    ]

用法：
    python report.py weekly    # 生成本周周报
    python report.py monthly   # 生成本月月报

环境变量：
    继承签到脚本的所有环境变量（SMTP、Telegram 等）
    REPORT_TELEGRAM   可选，设为 true 时通过 Telegram 发送报告（需 NOTIFY_CHAT_ID）
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# 复用签到项目的日志风格
from rich.console import Console
from rich.markdown import Markdown

_ansi_console = Console(force_terminal=True, color_system="auto", soft_wrap=True)


def _markdown_to_ansi(text):
    with _ansi_console.capture() as capture:
        _ansi_console.print(Markdown(text), end="")
    return capture.get().rstrip("\n")


class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record):
        original_levelname = record.levelname
        original_msg = record.msg
        original_args = record.args

        color = self.COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        record.msg = _markdown_to_ansi(record.getMessage())
        record.args = None

        formatted = super().format(record)

        record.levelname = original_levelname
        record.msg = original_msg
        record.args = original_args

        return formatted


class StripMarkdownFormatter(logging.Formatter):
    def format(self, record):
        import re
        formatted = super().format(record)
        formatted = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', formatted)
        formatted = re.sub(r'\*\*(.+?)\*\*', r'\1', formatted)
        formatted = re.sub(r'`(.+?)`', r'\1', formatted)
        formatted = re.sub(r'\*(.+?)\*', r'\1', formatted)
        return formatted


_log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(ColorFormatter(_log_fmt))
_file = logging.FileHandler("checkin.log", encoding="utf-8")
_file.setFormatter(StripMarkdownFormatter(_log_fmt))
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logger = logging.getLogger("tg-checkin")

HISTORY_FILE = os.getenv("HISTORY_FILE", "checkin_history.json")

# Telegram 通知
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "")


# ---------------------------------------------------------------------------
# 积分解析
# ---------------------------------------------------------------------------

# 各种积分格式的正则模式
# 1. 获得 X 积分：  "获得 **23** 积分" / "获得 10 积分" / "获得了 2 积分"
# 2. 当前余额：     "当前余额：**1546** 积分" / "当前余额 1546 积分"
# 3. 总分：         "总分: 1509"
# 4. 米币余额：     "当前余额 | 1551 米币"
# 5. 签到成功，获得 X 积分： "签到成功，获得 10 积分"

_RE_GAINED_POINTS = [
    re.compile(r'获得\*{0,2}\s*(\d+)\s*\*{0,2}\s*积分'),   # 获得 10 积分 / 获得 **10** 积分
    re.compile(r'获得\s+了?\s*\*{0,2}\s*(\d+)\s*\*{0,2}\s*积分'),  # 获得了 2 积分
    re.compile(r'签到成功.*?获得\*{0,2}\s*(\d+)\s*\*{0,2}\s*积分'),  # 签到成功，获得 10 积分
]

_RE_BALANCE_POINTS = [
    re.compile(r'当前余额[：:]\s*\*{0,2}\s*(\d+)\s*\*{0,2}\s*积分'),  # 当前余额：**1546** 积分
    re.compile(r'当前余额\*{0,2}\s*[：:|]?\s*\*{0,2}\s*(\d+)\s*\*{0,2}\s*(?:积分|米币)'),  # **当前余额** | 1551 米币
    re.compile(r'总分[：:]\s*(\d+)'),  # 总分: 1509
    re.compile(r'余额[：:]\s*\*{0,2}\s*(\d+)\s*\*{0,2}\s*(?:积分|米币|点)'),  # 余额：100 积分
]


def parse_points(message):
    """从机器人回复消息中解析积分信息

    支持的格式：
        - 获得 X 积分 / 获得 **X** 积分 / 获得了 X 积分
        - 当前余额：X 积分 / 当前余额 | X 米币
        - 总分: X
        - 签到成功，获得 X 积分

    Args:
        message: 机器人回复的文本

    Returns:
        dict: {"gained": int 或 None, "balance": int 或 None}
    """
    if not message:
        return {"gained": None, "balance": None}

    gained = None
    balance = None

    for pattern in _RE_GAINED_POINTS:
        m = pattern.search(message)
        if m:
            gained = int(m.group(1))
            break

    for pattern in _RE_BALANCE_POINTS:
        m = pattern.search(message)
        if m:
            balance = int(m.group(1))
            break

    return {"gained": gained, "balance": balance}


# ---------------------------------------------------------------------------
# 历史记录读写
# ---------------------------------------------------------------------------

def load_history():
    """加载历史签到记录"""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("❌ 读取历史记录失败: %s", e)
        return []


def save_history(records):
    """保存历史签到记录"""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("❌ 保存历史记录失败: %s", e)


def append_history(results):
    """将一次签到结果追加到历史记录中

    Args:
        results: checkin.py 中 do_checkin 返回的结果列表
    """
    records = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    success_count = sum(1 for r in results if r["success"])

    record = {
        "date": today,
        "timestamp": now,
        "bots": [
            {
                "name": r["name"],
                "username": r["username"],
                "success": r["success"],
                "message": r.get("notify_message") or r["message"],
                "points": parse_points(r.get("notify_message") or r["message"]),
            }
            for r in results
        ],
        "success_count": success_count,
        "total": len(results),
    }

    # 避免同一天重复记录（覆盖当天已有的）
    records = [r for r in records if r["date"] != today]
    records.append(record)
    records.sort(key=lambda r: r["date"])
    save_history(records)
    logger.info("📝 签到记录已保存到 %s（共 %d 天）", HISTORY_FILE, len(records))


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def _get_week_range(ref_date=None):
    """获取 ref_date 所在周的范围（周一到周日）

    返回 (start_date, end_date)，均为 date 对象。
    """
    if ref_date is None:
        ref_date = datetime.now().date()
    # weekday(): 0=Monday ... 6=Sunday
    start = ref_date - timedelta(days=ref_date.weekday())
    end = start + timedelta(days=6)
    return start, end


def _get_month_range(ref_date=None):
    """获取 ref_date 所在月的范围

    返回 (start_date, end_date)，均为 date 对象。
    """
    if ref_date is None:
        ref_date = datetime.now().date()
    start = ref_date.replace(day=1)
    # 月末：下个月1号减1天
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1) - timedelta(days=1)
    return start, end


def _filter_records(records, start_date, end_date):
    """筛选日期范围内的记录"""
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    return [r for r in records if start_str <= r["date"] <= end_str]


def _build_bot_stats(records):
    """统计每个机器人的成功率和积分

    返回列表，每项：{name, username, success, fail, total, rate,
                    total_gained, first_balance, last_balance, balance_change}
    """
    bot_map = {}  # key: username
    for rec in records:
        for bot in rec.get("bots", []):
            key = bot["username"]
            if key not in bot_map:
                bot_map[key] = {
                    "name": bot["name"],
                    "username": bot["username"],
                    "success": 0,
                    "fail": 0,
                    "total_gained": 0,
                    "first_balance": None,
                    "last_balance": None,
                }
            if bot["success"]:
                bot_map[key]["success"] += 1
            else:
                bot_map[key]["fail"] += 1

            # 积分统计
            points = bot.get("points") or parse_points(bot.get("message", ""))
            if points:
                gained = points.get("gained")
                balance = points.get("balance")
                if gained is not None:
                    bot_map[key]["total_gained"] += gained
                if balance is not None:
                    if bot_map[key]["first_balance"] is None:
                        bot_map[key]["first_balance"] = balance
                    bot_map[key]["last_balance"] = balance

    result = []
    for bot in bot_map.values():
        total = bot["success"] + bot["fail"]
        bot["total"] = total
        bot["rate"] = f"{bot['success'] / total * 100:.0f}%" if total else "0%"
        # 积分变化 = 最新余额 - 首次余额
        if bot["first_balance"] is not None and bot["last_balance"] is not None:
            bot["balance_change"] = bot["last_balance"] - bot["first_balance"]
        else:
            bot["balance_change"] = None
        result.append(bot)
    result.sort(key=lambda b: b["name"])
    return result


def _build_daily_points(records):
    """构建每日积分汇总

    返回列表，每项：{date, gained, balance_info}
    balance_info: {bot_name: balance} 各机器人当日最新余额
    """
    daily = []
    for rec in records:
        gained_total = 0
        balance_info = {}
        for bot in rec.get("bots", []):
            points = bot.get("points") or parse_points(bot.get("message", ""))
            if points:
                g = points.get("gained")
                b = points.get("balance")
                if g is not None:
                    gained_total += g
                if b is not None:
                    balance_info[bot["name"]] = b
        daily.append({
            "date": rec["date"],
            "gained": gained_total,
            "balance_info": balance_info,
        })
    return daily


def generate_report(records, report_type, start_date, end_date):
    """生成报告文本（Markdown 格式）

    Args:
        records: 筛选后的记录列表
        report_type: "周报" 或 "月报"
        start_date: 开始日期
        end_date: 结束日期

    Returns:
        (title, markdown_text)
    """
    period_str = f"{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}"
    title = f"TG 签到{report_type} {period_str}"

    if not records:
        return title, f"## {title}\n\n⚠️ 本时段内无签到记录。"

    total_days = len(records)
    total_bots_run = sum(r["total"] for r in records)
    total_success = sum(r["success_count"] for r in records)
    total_fail = total_bots_run - total_success
    overall_rate = f"{total_success / total_bots_run * 100:.1f}%" if total_bots_run else "0%"

    # 每日摘要（含积分）
    daily_points = _build_daily_points(records)
    daily_lines = []
    for i, rec in enumerate(records):
        mark = "✅" if rec["success_count"] == rec["total"] else "⚠️" if rec["success_count"] > 0 else "❌"
        dp = daily_points[i]
        gained_str = f"+{dp['gained']}" if dp["gained"] else "-"
        # 各机器人余额
        balance_parts = []
        for bname, bval in dp["balance_info"].items():
            balance_parts.append(f"{bname}: {bval}")
        balance_str = " | ".join(balance_parts) if balance_parts else "-"
        daily_lines.append(
            f"| {rec['date']} | {mark} {rec['success_count']}/{rec['total']} | {gained_str} | {balance_str} |"
        )

    # 机器人统计（含积分）
    bot_stats = _build_bot_stats(records)
    bot_lines = []
    for b in bot_stats:
        bot_mark = "✅" if b["fail"] == 0 else "⚠️" if b["success"] > 0 else "❌"
        gained_str = f"+{b['total_gained']}" if b["total_gained"] else "-"
        if b["first_balance"] is not None and b["last_balance"] is not None:
            change = b["balance_change"]
            change_str = f"{b['first_balance']} → {b['last_balance']} ({'+' if change >= 0 else ''}{change})"
        else:
            change_str = "-"
        bot_lines.append(
            f"| {bot_mark} {b['name']} | @{b['username']} | {b['success']} | {b['fail']} | {b['total']} | {b['rate']} | {gained_str} | {change_str} |"
        )

    # 积分总览
    total_gained = sum(dp["gained"] for dp in daily_points)
    # 各机器人最新余额汇总
    latest_balances = {}
    for dp in daily_points:
        for bname, bval in dp["balance_info"].items():
            latest_balances[bname] = bval

    # 失败记录详情
    fail_details = []
    for rec in records:
        for bot in rec.get("bots", []):
            if not bot["success"]:
                msg = bot["message"][:80] + "..." if len(bot["message"]) > 80 else bot["message"]
                fail_details.append(f"- **{rec['date']}** {bot['name']} (@{bot['username']}): {msg}")

    # 构建积分变化趋势文本（简单文本柱状图）
    trend_lines = []
    for dp in daily_points:
        gained = dp["gained"]
        bar = "█" * min(gained, 50) if gained > 0 else "▏"
        trend_lines.append(f"| {dp['date']} | `{bar}` +{gained} |")

    md = f"""## {title}

### 📊 总览

| 指标 | 数值 |
|------|------|
| 签到天数 | {total_days} 天 |
| 签到总次数 | {total_bots_run} 次 |
| 成功次数 | ✅ {total_success} 次 |
| 失败次数 | ❌ {total_fail} 次 |
| 总成功率 | **{overall_rate}** |
| 本期总获得积分 | **+{total_gained}** |

### 📅 每日签到详情

| 日期 | 结果 | 获得积分 | 余额 |
|------|------|----------|------|
{chr(10).join(daily_lines)}

### 📈 积分变化趋势

| 日期 | 获得积分 |
|------|----------|
{chr(10).join(trend_lines)}

### 🤖 各机器人统计

| 机器人 | 用户名 | 成功 | 失败 | 总计 | 成功率 | 获得积分 | 余额变化 |
|--------|--------|------|------|------|--------|----------|----------|
{chr(10).join(bot_lines)}
"""

    if latest_balances:
        balance_summary = " | ".join(f"{k}: **{v}**" for k, v in latest_balances.items())
        md += f"""
### 💰 最新余额

{balance_summary}
"""

    if fail_details:
        md += f"""
### ❌ 失败记录

{chr(10).join(fail_details)}
"""

    md += f"""
---
*本报告由 TG 签到助手自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""
    return title, md


# ---------------------------------------------------------------------------
# HTML 报告（用于邮件）
# ---------------------------------------------------------------------------

def _md_table_to_html(md_text):
    """简易 Markdown 转 HTML（用于邮件，保留表格和标题）"""
    import html as html_module
    import re

    # HTML 转义
    text = html_module.escape(md_text)

    # 标题
    text = re.sub(r'## (.+)', r'<h2>\1</h2>', text)
    text = re.sub(r'### (.+)', r'<h3>\1</h3>', text)

    # 分隔线
    text = re.sub(r'^---$', r'<hr>', text, flags=re.MULTILINE)

    # 斜体
    text = re.sub(r'\*([^*]+?)\*', r'<em>\1</em>', text)

    # 粗体
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)

    # 表格处理
    lines = text.split("\n")
    html_lines = []
    in_table = False
    is_header = True

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # 跳过分隔行

            if not in_table:
                html_lines.append('<table style="border-collapse:collapse;width:100%;font-size:14px;margin:12px 0;">')
                in_table = True
                is_header = True

            tag = "th" if is_header else "td"
            style = ' style="padding:8px 12px;border:1px solid #ddd;{}"'.format(
                "background:#f5f5f5;text-align:left;" if is_header else "text-align:left;"
            )
            cells_html = "".join(f"<{tag}{style}>{c}</{tag}>" for c in cells)
            html_lines.append(f"<tr>{cells_html}</tr>")
            is_header = False
        else:
            if in_table:
                html_lines.append("</table>")
                in_table = False
            if stripped:
                html_lines.append(f"<p>{stripped}</p>")

    if in_table:
        html_lines.append("</table>")

    return "\n".join(html_lines)


def build_report_email_html(title, md_text):
    """构建报告的 HTML 邮件正文"""
    body_html = _md_table_to_html(md_text)
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;color:#333;max-width:800px;margin:0 auto;">
{body_html}
</body>
</html>"""


def build_report_plain(title, md_text):
    """构建报告的纯文本正文"""
    import re
    text = md_text
    # 去除 Markdown 语法
    text = re.sub(r'#+ (.+)', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^---$', '-' * 40, text, flags=re.MULTILINE)
    return text


# ---------------------------------------------------------------------------
# 报告发送
# ---------------------------------------------------------------------------

async def send_report_email(title, md_text):
    """通过邮件发送报告"""
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
    smtp_ssl = (os.getenv("SMTP_SSL") or "true").strip().lower() in ("true", "1", "yes")
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_addr = (os.getenv("SMTP_FROM") or username).strip()
    from_name = (os.getenv("SMTP_FROM_NAME") or "TG 签到助手").strip()
    to_raw = os.getenv("EMAIL_TO", "").strip()

    if not smtp_host or not username or not password or not to_raw:
        logger.error("❌ 邮件配置不完整，跳过发送报告邮件")
        return

    to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr

    msg = MIMEMultipart("alternative")
    msg["Subject"] = title
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = ", ".join(to_addrs)

    plain = build_report_plain(title, md_text)
    html = build_report_email_html(title, md_text)

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        import aiosmtplib
    except ImportError:
        logger.error("❌ 未安装 aiosmtplib，跳过发送报告邮件")
        return

    try:
        logger.info("📧 正在发送%s到 %s ...", title, ", ".join(to_addrs))
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=username,
            password=password,
            use_tls=smtp_ssl,
            start_tls=not smtp_ssl,
            timeout=30,
        )
        logger.info("🔔 %s已发送至 %s", title, ", ".join(to_addrs))
    except Exception as e:
        logger.error("❌ %s发送失败: %s", title, e)


async def send_report_telegram(md_text):
    """通过 Telegram 发送报告"""
    if not NOTIFY_CHAT_ID:
        return

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    API_ID = int(os.getenv("API_ID", "0") or "0")
    API_HASH = os.getenv("API_HASH", "")
    SESSION_STRING = os.getenv("SESSION_STRING", "")

    if not API_ID or not API_HASH or not SESSION_STRING:
        logger.error("❌ Telegram 配置不完整，跳过发送报告通知")
        return

    from proxy import get_proxy
    proxy = get_proxy()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, proxy=proxy)
    await client.connect()

    if not await client.is_user_authorized():
        logger.error("❌ SESSION_STRING 无效，跳过发送报告通知")
        await client.disconnect()
        return

    # Telegram 消息长度限制 4096，截断处理
    text = md_text
    if len(text) > 4000:
        text = text[:3990] + "\n..."

    try:
        await client.send_message(NOTIFY_CHAT_ID, text)
        logger.info("🔔 报告已发送至 Telegram")
    except Exception as e:
        logger.error("❌ Telegram 报告发送失败: %s", e)
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("weekly", "monthly"):
        print("用法: python report.py weekly|monthly")
        sys.exit(1)

    report_type_arg = sys.argv[1]
    report_type = "周报" if report_type_arg == "weekly" else "月报"

    records = load_history()
    if not records:
        logger.error("❌ 无历史签到记录（%s 不存在或为空）", HISTORY_FILE)
        sys.exit(1)

    if report_type_arg == "weekly":
        start_date, end_date = _get_week_range()
    else:
        start_date, end_date = _get_month_range()

    filtered = _filter_records(records, start_date, end_date)
    logger.info("📋 生成%s: %s ~ %s，共 %d 天记录",
                report_type, start_date, end_date, len(filtered))

    title, md_text = generate_report(filtered, report_type, start_date, end_date)

    # 输出到控制台
    print()
    print(md_text)

    # 发送邮件
    await send_report_email(title, md_text)

    # 可选：通过 Telegram 发送
    report_tg = os.getenv("REPORT_TELEGRAM", "").strip().lower() in ("true", "1", "yes")
    if report_tg:
        await send_report_telegram(md_text)


if __name__ == "__main__":
    asyncio.run(main())
