#!/usr/bin/env python3
"""
图片验证码识别测试脚本

用法:
    python test_image_solver.py <图片路径> [选项1] [选项2] [选项3] [选项4]

示例:
    python test_image_solver.py test.png 鞋子 美女 手机 水杯
    python test_image_solver.py test.png 苹果 香蕉 橙子 西瓜
"""
import asyncio
import base64
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

from image_solver import solve_image_captcha

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tg-checkin")


async def test_image_recognition(image_path: str, options: list[str]):
    """测试图片识别功能"""
    
    # 读取图片
    image_file = Path(image_path)
    if not image_file.exists():
        logger.error(f"图片不存在: {image_path}")
        return
    
    # 读取并转为 base64
    with open(image_file, "rb") as f:
        image_data = f.read()
    image_b64 = base64.b64encode(image_data).decode("utf-8")
    
    # 检测 MIME 类型
    mime_type = "image/jpeg"
    if image_path.lower().endswith(".png"):
        mime_type = "image/png"
    elif image_path.lower().endswith(".webp"):
        mime_type = "image/webp"
    
    logger.info(f"测试图片: {image_path}")
    logger.info(f"图片大小: {len(image_data)} bytes")
    logger.info(f"MIME类型: {mime_type}")
    logger.info(f"选项: {' | '.join(options)}")
    logger.info("-" * 50)
    
    # 测试 Gemini
    logger.info("\n>>> 使用 Gemini 识别...")
    result_gemini = await solve_image_captcha(
        image_b64, mime_type, options, 
        provider="gemini",
        debug=True,  # 开启详细日志
    )
    logger.info(f"Gemini 结果: {result_gemini}")
    
    # 测试 OpenAI (如果配置了 Key)
    import os
    if os.getenv("OPENAI_API_KEY"):
        logger.info("\n>>> 使用 OpenAI 识别...")
        result_openai = await solve_image_captcha(
            image_b64, mime_type, options,
            provider="openai"
        )
        logger.info(f"OpenAI 结果: {result_openai}")
    else:
        logger.info("\n>>> 跳过 OpenAI 测试 (未配置 OPENAI_API_KEY)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    image_path = sys.argv[1]
    
    # 默认选项：模拟常见的验证码选项
    default_options = ["鞋子", "美女", "手机", "水杯"]
    
    # 如果提供了选项，使用提供的；否则用默认
    if len(sys.argv) >= 3:
        options = sys.argv[2:6]  # 最多取4个选项
    else:
        options = default_options
        logger.info(f"未提供选项，使用默认: {' | '.join(options)}")
    
    asyncio.run(test_image_recognition(image_path, options))


if __name__ == "__main__":
    main()
