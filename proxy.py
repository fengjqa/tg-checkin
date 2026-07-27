#!/usr/bin/env python3
"""
代理配置模块

从环境变量读取代理设置，返回 Telethon 可用的 proxy 参数。
不配置 PROXY_HOST 时不启用代理，保持原有行为不变。

Telethon 异步模式需要 python-socks 库（不是 PySocks）。
安装：pip install python-socks[asyncio]

支持的环境变量：
    PROXY_TYPE     socks5 | socks4 | http   默认 socks5
    PROXY_HOST     代理服务器地址            留空则不启用代理
    PROXY_PORT     代理服务器端口
    PROXY_USERNAME 可选，认证用户名
    PROXY_PASSWORD 可选，认证密码
    PROXY_RDNS     可选，是否远程 DNS 解析，默认 true
"""
import os

# python-socks 的代理类型映射
_PROXY_TYPES = {
    "socks5": "socks5",
    "socks4": "socks4",
    "http": "http",
}


def get_proxy():
    """返回 Telethon 的 proxy 参数，未配置代理时返回 None

    返回格式为 python-socks 兼容的元组：
        (type_str, host, port, rdns)               无认证
        (type_str, host, port, rdns, username, password)  有认证
    """
    host = os.getenv("PROXY_HOST", "").strip()
    if not host:
        return None

    port = int(os.getenv("PROXY_PORT", "0") or "0")
    if not port:
        return None

    proxy_type = os.getenv("PROXY_TYPE", "socks5").strip().lower()
    username = os.getenv("PROXY_USERNAME", "").strip() or None
    password = os.getenv("PROXY_PASSWORD", "").strip() or None
    rdns = os.getenv("PROXY_RDNS", "true").strip().lower() in ("true", "1", "yes")

    if proxy_type not in _PROXY_TYPES:
        raise ValueError(
            f"不支持的 PROXY_TYPE: {proxy_type}，可选: socks5 / socks4 / http"
        )

    # 检查 python-socks 是否已安装
    try:
        import python_socks  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "已配置代理但未安装 python-socks，请运行: pip install python-socks[asyncio]"
        )

    # 使用字符串类型，python-socks 兼容此格式
    proxy = (_PROXY_TYPES[proxy_type], host, port, rdns)

    # 只有提供了用户名或密码时才附加认证信息
    if username or password:
        proxy = proxy + (username, password)

    return proxy


def proxy_info_str():
    """返回代理的可读描述，用于日志输出"""
    host = os.getenv("PROXY_HOST", "").strip()
    if not host:
        return "直连（未启用代理）"

    port = os.getenv("PROXY_PORT", "").strip()
    proxy_type = os.getenv("PROXY_TYPE", "socks5").strip().lower()
    username = os.getenv("PROXY_USERNAME", "").strip()

    auth = f"{username}@" if username else ""
    return f"{proxy_type}://{auth}{host}:{port}"
