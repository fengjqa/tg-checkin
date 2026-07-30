#!/usr/bin/env python3
"""
邮件通知模块

在签到完成后，将结果汇总通过邮件发送。
使用 aiosmtplib 实现异步发送，不阻塞主流程。

支持的环境变量：
    EMAIL_ENABLED   是否启用邮件通知 (true/false)，默认 false
    SMTP_HOST       SMTP 服务器地址，如 smtp.gmail.com
    SMTP_PORT       SMTP 端口，默认 465（SSL）
    SMTP_SSL        是否使用 SSL，默认 true；设为 false 时用 STARTTLS
    SMTP_USERNAME   发件邮箱账号
    SMTP_PASSWORD   发件邮箱密码或应用专用密码
    SMTP_FROM       发件人地址，默认同 SMTP_USERNAME
    SMTP_FROM_NAME  发件人显示名称，默认 "TG 签到助手"
    EMAIL_TO        收件人地址，多个用逗号分隔

常见 SMTP 配置参考：
    Gmail:      smtp.gmail.com  端口 465 (SSL)  需用应用专用密码
    QQ 邮箱:    smtp.qq.com     端口 465 (SSL)  需用授权码
    163 邮箱:   smtp.163.com    端口 465 (SSL)  需用授权码
    Outlook:    smtp-mail.outlook.com 端口 587 (STARTTLS)
"""
import asyncio
import html
import logging
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import markdown as md_lib

logger = logging.getLogger("tg-checkin")


def is_email_enabled():
    """检查是否启用了邮件通知"""
    return os.getenv("EMAIL_ENABLED", "").strip().lower() in ("true", "1", "yes")


def _markdown_to_html(text):
    """将 Markdown 语法转换为 HTML（用于邮件正文）

    使用 markdown 库进行转换，支持完整的 Markdown 语法。
    """
    # 1. HTML 转义（防止 XSS）
    escaped = html.escape(text)
    # 2. Markdown → HTML（nl2br 扩展将换行转为 <br>）
    html_content = md_lib.markdown(escaped, extensions=["nl2br"])
    # 3. 去除块级标签（<p> 等），只保留内联格式（邮件中在表格单元格内）
    for tag in ("p", "div", "ul", "ol", "li"):
        html_content = html_content.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
    return html_content.strip()


def _strip_markdown(text):
    """去除 Markdown 语法符号（用于纯文本邮件）

    将 [text](url) 转为 text (url)，去除 ** * ` 等标记。
    """
    # 链接 [text](url) → text (url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    # 粗体 **text** → text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # 代码 `text` → text
    text = re.sub(r'`(.+?)`', r'\1', text)
    # 斜体 *text* → text
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    return text


def _build_summary_html(results):
    """构建 HTML 格式的签到结果摘要"""
    success_count = sum(1 for r in results if r["success"])
    total = len(results)

    rows = []
    for r in results:
        mark = "✅" if r["success"] else "❌"
        status = "成功" if r["success"] else "失败"
        color = "#28a745" if r["success"] else "#dc3545"
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px;'>{mark}</td>"
            f"<td style='padding:6px 12px;'>{r['name']}</td>"
            f"<td style='padding:6px 12px;'>@{r['username']}</td>"
            f"<td style='padding:6px 12px; color:{color};'>{status}</td>"
            f"<td style='padding:6px 12px;'>{_markdown_to_html(r.get('notify_message') or r['message'])}</td>"
            f"</tr>"
        )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; color: #333;">
  <h2 style="margin-bottom: 4px;">📋 TG 签到结果</h2>
  <p style="color: #666; margin: 0 0 16px;">
    {success_count}/{total} 成功 · {total - success_count} 失败
  </p>
  <table style="border-collapse: collapse; width: 100%; font-size: 14px;">
    <thead>
      <tr style="background: #f5f5f5; text-align: left;">
        <th style="padding: 8px 12px;">状态</th>
        <th style="padding: 8px 12px;">名称</th>
        <th style="padding: 8px 12px;">用户名</th>
        <th style="padding: 8px 12px;">结果</th>
        <th style="padding: 8px 12px;">详情</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  <p style="color: #999; font-size: 12px; margin-top: 16px;">
    本邮件由 TG 签到脚本自动发送
  </p>
</body>
</html>"""
    return html


def _build_summary_plain(results):
    """构建纯文本格式的签到结果摘要"""
    success_count = sum(1 for r in results if r["success"])
    total = len(results)

    lines = [f"TG 签到结果 {success_count}/{total}"]
    for r in results:
        mark = "✅" if r["success"] else "❌"
        lines.append(f"{mark} {r['name']} (@{r['username']}): {_strip_markdown(r.get('notify_message') or r['message'])}")
    return "\n".join(lines)


async def send_email_notify(results):
    """发送邮件通知

    Args:
        results: 签到结果列表，每个元素为 dict，包含 name, username, success, message
    """
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
    smtp_ssl = (os.getenv("SMTP_SSL") or "true").strip().lower() in ("true", "1", "yes")
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_addr = (os.getenv("SMTP_FROM") or username).strip()
    from_name = (os.getenv("SMTP_FROM_NAME") or "TG 签到助手").strip()
    to_raw = os.getenv("EMAIL_TO", "").strip()

    # 参数校验
    if not smtp_host:
        logger.error("❌ 邮件通知失败：未配置 SMTP_HOST")
        return
    if not username or not password:
        logger.error("❌ 邮件通知失败：未配置 SMTP_USERNAME 或 SMTP_PASSWORD")
        return
    if not to_raw:
        logger.error("❌ 邮件通知失败：未配置 EMAIL_TO")
        return

    to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]
    if not to_addrs:
        logger.error("❌ 邮件通知失败：EMAIL_TO 格式无效")
        return

    success_count = sum(1 for r in results if r["success"])
    total = len(results)

    # 构建邮件
    subject = f"TG 签到结果 {success_count}/{total}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = ", ".join(to_addrs)

    plain_text = _build_summary_plain(results)
    html_text = _build_summary_html(results)

    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_text, "html", "utf-8"))

    # 发送邮件
    try:
        import aiosmtplib
    except ImportError:
        logger.error("❌ 邮件通知失败：未安装 aiosmtplib，请运行 pip install aiosmtplib")
        return

    try:
        logger.info("📧 正在发送邮件通知到 %s ...", ", ".join(to_addrs))

        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=username,
            password=password,
            use_tls=smtp_ssl,          # SSL (port 465)
            start_tls=not smtp_ssl,    # STARTTLS (port 587)
            timeout=20,
        )

        logger.info("🔔 邮件通知已发送至 %s", ", ".join(to_addrs))
    except asyncio.TimeoutError:
        logger.error("❌ 邮件发送超时（20秒）")
    except Exception as e:  # noqa: BLE001
        logger.error("❌ 邮件发送失败: %s", e)
