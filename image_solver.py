#!/usr/bin/env python3
"""
图片验证码识别模块

支持两种 AI 视觉 API：
  - Google Gemini（默认，有免费额度）：https://aistudio.google.com/apikey
  - OpenAI GPT-4o（付费，但识别能力更强）：https://platform.openai.com/api-keys

通过环境变量配置 API Key：
  - Gemini: GEMINI_API_KEY
  - OpenAI: OPENAI_API_KEY

可在 config.yaml 中按机器人指定提供商和模型：
  ai_provider: gemini          # 或 openai
  ai_model: gemini-2.5-flash   # 覆盖默认模型
"""
import asyncio
import base64
import logging
import os
import re

import aiohttp

logger = logging.getLogger("tg-checkin")

# ---- 全局默认配置（环境变量）----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL_DEFAULT = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_DEFAULT = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _is_thinking_model(model_name):
    """判断模型是否为思考型（Gemini 2.5+ / 3.x）

    思考型模型会将内部推理 token 计入 maxOutputTokens 预算，
    若 maxOutputTokens 过小，模型会把所有 token 用于"思考"，
    导致 content.parts 为空、finishReason=MAX_TOKENS。
    """
    m = re.match(r"gemini-(\d+)", model_name)
    if m:
        major = int(m.group(1))
        if major >= 3:
            return True
    if "2.5" in model_name:
        return True
    return False


def _build_thinking_config(model_name):
    """根据模型版本构建 thinkingConfig，禁用/最小化思考

    - Gemini 2.5: thinkingBudget=0（完全禁用思考）
    - Gemini 3+:  thinkingLevel="MINIMAL"（最低思考档位）
    """
    m = re.match(r"gemini-(\d+)", model_name)
    if m:
        major = int(m.group(1))
        if major >= 3:
            # Gemini 3+ 使用 thinkingLevel
            return {"thinkingLevel": "MINIMAL"}
    # Gemini 2.5 使用 thinkingBudget（0 = 完全禁用）
    return {"thinkingBudget": 0}


def _get_proxy_url():
    """根据环境变量构建代理 URL，无代理时返回 None"""
    host = os.getenv("PROXY_HOST", "").strip()
    if not host:
        return None

    port = os.getenv("PROXY_PORT", "0").strip()
    if not port:
        return None

    proxy_type = os.getenv("PROXY_TYPE", "socks5").strip().lower()
    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()

    auth = f"{username}:{password}@" if username or password else ""
    return f"{proxy_type}://{auth}{host}:{port}"


def _build_proxy_connector():
    """根据环境变量构建 aiohttp 代理连接器，无代理时返回 None

    复用 proxy.py 的环境变量配置，确保本地运行时 AI API 也走代理。
    """
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return None

    # 尝试多种方式导入 ProxyConnector（兼容不同版本的 python-socks）
    try:
        # 方式1: python-socks 2.x
        from python_socks.async_.asyncio import ProxyConnector
        return ProxyConnector.from_url(proxy_url)
    except Exception:  # noqa: BLE001
        pass

    try:
        # 方式2: aiohttp-socks（如果安装了的话）
        from aiohttp_socks import ProxyConnector
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        return ProxyConnector(
            proxy_type=parsed.scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )
    except Exception:  # noqa: BLE001
        pass

    logger.warning("\u26a0\ufe0f 无法构建代理连接器，将尝试直连 AI API")
    return None


def _get_aiohttp_session():
    """创建 aiohttp 会话，自动处理代理"""
    connector = _build_proxy_connector()
    if connector:
        return aiohttp.ClientSession(connector=connector)
    # 无代理时，使用 trust_env=True 允许 aiohttp 读取环境变量中的代理设置
    return aiohttp.ClientSession(trust_env=True)


async def download_image_as_base64(client, message, debug=False):
    """从 Telegram 消息中下载图片

    返回 (base64_str, mime_type)，无图片时返回 (None, None)。
    """
    try:
        if message.photo:
            if debug:
                photo = message.photo
                logger.debug(
                    "[image_solver] 检测到图片 photo_id=%s, sizes=%d, "
                    "w=%d, h=%d",
                    photo.id if hasattr(photo, 'id') else 'unknown',
                    len(photo.sizes) if hasattr(photo, 'sizes') else 0,
                    photo.sizes[-1].w if hasattr(photo, 'sizes') and photo.sizes else 0,
                    photo.sizes[-1].h if hasattr(photo, 'sizes') and photo.sizes else 0,
                )
            data = await client.download_media(message, file=bytes)
            if debug:
                logger.debug("[image_solver] 图片下载完成: %d bytes", len(data))
            return base64.b64encode(data).decode("utf-8"), "image/jpeg"
        elif message.document:
            mime_type = message.document.mime_type or ""
            if mime_type.startswith("image/"):
                if debug:
                    logger.debug(
                        "[image_solver] 检测到图片文档: %s, size=%s",
                        mime_type,
                        message.document.size if hasattr(message.document, 'size') else 'unknown',
                    )
                data = await client.download_media(message, file=bytes)
                if debug:
                    logger.debug("[image_solver] 图片文档下载完成: %d bytes", len(data))
                return base64.b64encode(data).decode("utf-8"), mime_type
            else:
                if debug:
                    logger.debug("[image_solver] 文档不是图片: %s", mime_type)
    except Exception as e:  # noqa: BLE001
        logger.error("\u274c 下载图片失败: %s", e)
    return None, None


def get_button_options(message):
    """提取消息中所有按钮文字，返回列表"""
    options = []
    if not message.buttons:
        return options
    for row in message.buttons:
        for btn in row:
            btn_text = (getattr(btn, "text", "") or "").strip()
            if btn_text:
                options.append(btn_text)
    return options


async def solve_image_captcha(
    image_b64, mime_type, options, provider=None, model=None, debug=False
):
    """调用 AI Vision API 识别图片，从选项中选出正确答案

    Args:
        image_b64: base64 编码的图片数据
        mime_type: 图片 MIME 类型（如 image/jpeg）
        options: 按钮选项文字列表
        provider: AI 提供商，"gemini" 或 "openai"，默认自动选择（谁配了 Key 用谁）
        model: 指定模型名，覆盖默认值
        debug: 是否输出详细调试日志

    Returns:
        匹配到的选项文字，失败返回 None
    """
    if debug:
        logger.info(
            "[image_solver] solve_image_captcha 开始: "
            "provider=%s, model=%s, image_size=%d bytes, options=%s",
            provider or "auto",
            model or "default",
            len(image_b64) if image_b64 else 0,
            options,
        )

    # 自动选择 provider：优先用参数指定的，否则看谁配了 Key
    if not provider:
        if GEMINI_API_KEY:
            provider = "gemini"
        elif OPENAI_API_KEY:
            provider = "openai"
        else:
            logger.error(
                "\u274c 未配置任何 AI API Key，请配置 GEMINI_API_KEY 或 OPENAI_API_KEY"
            )
            return None

    if debug:
        logger.info("[image_solver] 使用 provider: %s", provider)

    if provider == "openai":
        return await _solve_with_openai(image_b64, mime_type, options, model, debug)
    else:
        return await _solve_with_gemini(image_b64, mime_type, options, model, debug)


async def _solve_with_gemini(image_b64, mime_type, options, model=None, debug=False):
    """使用 Google Gemini API 识别图片"""
    api_key = GEMINI_API_KEY
    if not api_key:
        logger.error("\u274c 未配置 GEMINI_API_KEY")
        return None

    model_name = model or GEMINI_MODEL_DEFAULT
    api_url = f"{GEMINI_API_BASE}/{model_name}:generateContent"

    if debug:
        logger.info("[image_solver] Gemini API URL: %s", api_url)
        logger.info("[image_solver] Gemini 模型: %s", model_name)

    options_text = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    prompt = (
        "请看这张图片，从以下选项中选出与图片内容最匹配的一个。\n"
        "只返回选项的完整文字内容，不要加序号、引号或其他说明。\n\n"
        f"选项：\n{options_text}"
    )

    if debug:
        logger.info("[image_solver] Gemini Prompt:\n%s", prompt)

    # Gemini 安全过滤设置：将所有类别设为 BLOCK_NONE，避免验证码图片被误拦截
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    generation_config = {
        "temperature": 0.1,
        "maxOutputTokens": 2048,
    }

    # 思考型模型（Gemini 2.5+ / 3.x）会消耗大量内部推理 token，
    # 导致实际输出为空。此处禁用/最小化思考，图片识别无需复杂推理。
    if _is_thinking_model(model_name):
        thinking_config = _build_thinking_config(model_name)
        generation_config["thinkingConfig"] = thinking_config
        if debug:
            logger.info(
                "[image_solver] 检测到思考型模型 %s，已设置 thinkingConfig=%s",
                model_name, thinking_config,
            )

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            ]
        }],
        "generationConfig": generation_config,
        "safetySettings": safety_settings,
    }

    proxy_url = _get_proxy_url()
    if debug:
        logger.info("[image_solver] 代理: %s", proxy_url or "无")

    try:
        async with _get_aiohttp_session() as session:
            if debug:
                logger.info("[image_solver] 正在发送请求到 Gemini API...")
            async with session.post(
                f"{api_url}?key={api_key}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
                proxy=proxy_url,
            ) as resp:
                if debug:
                    logger.info("[image_solver] Gemini API 响应状态: %d", resp.status)

                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        "\u274c Gemini API 返回错误 %d: %s",
                        resp.status, error_text[:500],
                    )
                    return None

                data = await resp.json()

                if debug:
                    logger.debug("[image_solver] Gemini API 原始响应: %s", str(data)[:500])

                candidates = data.get("candidates", [])
                if not candidates:
                    logger.error("\u274c Gemini API 未返回 candidates")
                    prompt_feedback = data.get("promptFeedback", {})
                    if prompt_feedback:
                        logger.error(
                            "\u274c promptFeedback: %s",
                            prompt_feedback.get("blockReason", "unknown"),
                        )
                    if debug:
                        logger.debug("[image_solver] 完整响应: %s", data)
                    return None

                # 检查 finishReason，诊断为何 content.parts 为空
                finish_reason = candidates[0].get("finishReason", "")
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    logger.error(
                        "\u274c Gemini API 返回 content.parts 为空 (finishReason=%s)",
                        finish_reason or "unknown",
                    )
                    if debug:
                        logger.debug("[image_solver] candidate: %s", candidates[0])
                    return None

                answer_text = parts[0].get("text", "").strip()
                logger.info(
                    "\U0001f916 Gemini(%s) 识别结果: %s", model_name, answer_text,
                )

                # 匹配过程
                matched = _match_answer(answer_text, options, debug)
                if debug:
                    logger.info(
                        "[image_solver] 匹配结果: %s -> %s",
                        answer_text, matched,
                    )
                return matched
    except asyncio.TimeoutError:
        logger.error("\u274c Gemini API 调用超时 (15秒)")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error("\u274c Gemini API 调用失败: %s", e)
        return None


async def _solve_with_openai(image_b64, mime_type, options, model=None, debug=False):
    """使用 OpenAI Vision API 识别图片"""
    api_key = OPENAI_API_KEY
    if not api_key:
        logger.error("\u274c 未配置 OPENAI_API_KEY")
        return None

    model_name = model or OPENAI_MODEL_DEFAULT

    if debug:
        logger.info("[image_solver] OpenAI API Base: %s", OPENAI_BASE_URL)
        logger.info("[image_solver] OpenAI 模型: %s", model_name)

    options_text = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    prompt = (
        "请看这张图片，从以下选项中选出与图片内容最匹配的一个。\n"
        "只返回选项的完整文字内容，不要加序号、引号或其他说明。\n\n"
        f"选项：\n{options_text}"
    )

    if debug:
        logger.info("[image_solver] OpenAI Prompt:\n%s", prompt)

    payload = {
        "model": model_name,
        "temperature": 0.1,
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_b64}",
                    },
                },
            ],
        }],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    proxy_url = _get_proxy_url()
    if debug:
        logger.info("[image_solver] 代理: %s", proxy_url or "无")

    try:
        async with _get_aiohttp_session() as session:
            if debug:
                logger.info("[image_solver] 正在发送请求到 OpenAI API...")
            async with session.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                proxy=proxy_url,
            ) as resp:
                if debug:
                    logger.info("[image_solver] OpenAI API 响应状态: %d", resp.status)

                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        "\u274c OpenAI API 返回错误 %d: %s",
                        resp.status, error_text[:500],
                    )
                    return None

                data = await resp.json()

                if debug:
                    logger.debug("[image_solver] OpenAI API 原始响应: %s", str(data)[:500])

                choices = data.get("choices", [])
                if not choices:
                    logger.error("\u274c OpenAI API 未返回 choices")
                    if debug:
                        logger.debug("[image_solver] 完整响应: %s", data)
                    return None

                answer_text = choices[0].get("message", {}).get("content", "").strip()
                logger.info(
                    "\U0001f916 OpenAI(%s) 识别结果: %s", model_name, answer_text,
                )

                # 匹配过程
                matched = _match_answer(answer_text, options, debug)
                if debug:
                    logger.info(
                        "[image_solver] 匹配结果: %s -> %s",
                        answer_text, matched,
                    )
                return matched
    except asyncio.TimeoutError:
        logger.error("\u274c OpenAI API 调用超时 (15秒)")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error("\u274c OpenAI API 调用失败: %s", e)
        return None


def _match_answer(answer_text, options, debug=False):
    """将 AI 返回的文字匹配到选项

    依次尝试：精确匹配 → 去序号匹配 → 大小写不敏感 → 包含匹配。
    """
    answer_text = answer_text.strip().strip("\"'""''")

    if debug:
        logger.info("[image_solver] 开始匹配: '%s'", answer_text)

    # 精确匹配
    for opt in options:
        if answer_text == opt:
            if debug:
                logger.info("[image_solver] 精确匹配成功: '%s' == '%s'", answer_text, opt)
            return opt

    # 去除序号前缀后匹配（如 "1. 手机" → "手机"）
    cleaned = re.sub(r"^\d+[.、\)]\s*", "", answer_text).strip()
    for opt in options:
        if cleaned == opt:
            if debug:
                logger.info("[image_solver] 去序号匹配成功: '%s' -> '%s' == '%s'", answer_text, cleaned, opt)
            return opt

    # 大小写不敏感匹配
    for opt in options:
        if answer_text.lower() == opt.lower():
            if debug:
                logger.info("[image_solver] 大小写匹配成功: '%s' ~= '%s'", answer_text, opt)
            return opt

    # 包含匹配
    for opt in options:
        if opt in answer_text or answer_text in opt:
            if debug:
                logger.info("[image_solver] 包含匹配成功: '%s' in '%s'", answer_text, opt)
            return opt

    logger.warning(
        "\u26a0\ufe0f AI 返回的答案未匹配到任何选项: '%s' (选项: %s)",
        answer_text, options,
    )
    if debug:
        logger.info("[image_solver] 所有匹配方式均失败")
    return None
