#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
列出 OpenAI 兼容 API 的可用模型

使用方法：
    python list_openai_models.py
    python list_openai_models.py --base-url https://api.openai.com/v1 --api-key YOUR_KEY
"""

import sys
import argparse
from collections import defaultdict

# Windows 控制台编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

try:
    from openai import OpenAI
except ImportError:
    print("❌ 缺少 openai 库，请安装: pip install openai")
    sys.exit(1)


def categorize_models(models):
    """将模型按类型分类"""
    categories = defaultdict(list)

    for model in models:
        model_id = model.id

        # 分类规则
        if "gpt-4" in model_id.lower():
            if "vision" in model_id.lower() or "turbo" in model_id.lower():
                categories["GPT-4 (Vision/Turbo)"].append(model_id)
            else:
                categories["GPT-4"].append(model_id)
        elif "gpt-3.5" in model_id.lower():
            categories["GPT-3.5"].append(model_id)
        elif "claude" in model_id.lower():
            if "opus" in model_id.lower():
                categories["Claude Opus"].append(model_id)
            elif "sonnet" in model_id.lower():
                categories["Claude Sonnet"].append(model_id)
            elif "haiku" in model_id.lower():
                categories["Claude Haiku"].append(model_id)
            else:
                categories["Claude (其他)"].append(model_id)
        elif "gemini" in model_id.lower():
            if "pro" in model_id.lower():
                categories["Gemini Pro"].append(model_id)
            elif "flash" in model_id.lower():
                categories["Gemini Flash"].append(model_id)
            else:
                categories["Gemini (其他)"].append(model_id)
        elif "dall-e" in model_id.lower() or "image" in model_id.lower():
            categories["图像生成"].append(model_id)
        elif "whisper" in model_id.lower() or "tts" in model_id.lower():
            categories["语音"].append(model_id)
        elif "embedding" in model_id.lower():
            categories["Embedding"].append(model_id)
        elif "text-" in model_id.lower():
            categories["文本模型 (旧版)"].append(model_id)
        else:
            categories["其他"].append(model_id)

    return categories


def list_models(base_url, api_key):
    """列出可用模型"""
    print(f"\n{'='*70}")
    print(f"查询 OpenAI 兼容 API 的可用模型")
    print(f"{'='*70}\n")
    print(f"Base URL: {base_url}")
    print(f"API Key: {api_key[:10]}...{api_key[-4:] if len(api_key) > 14 else '****'}\n")

    try:
        # 创建客户端
        client = OpenAI(api_key=api_key, base_url=base_url)

        # 获取模型列表
        print("正在获取模型列表...\n")
        models = client.models.list()

        if not models.data:
            print("⚠️  未找到任何模型")
            return

        print(f"✅ 找到 {len(models.data)} 个模型\n")

        # 分类显示
        categories = categorize_models(models.data)

        # 按类别排序
        category_order = [
            "GPT-4 (Vision/Turbo)",
            "GPT-4",
            "GPT-3.5",
            "Claude Opus",
            "Claude Sonnet",
            "Claude Haiku",
            "Claude (其他)",
            "Gemini Pro",
            "Gemini Flash",
            "Gemini (其他)",
            "图像生成",
            "语音",
            "Embedding",
            "文本模型 (旧版)",
            "其他",
        ]

        for category in category_order:
            if category in categories:
                models_in_category = sorted(categories[category])
                print(f"📦 {category} ({len(models_in_category)} 个)")
                print(f"{'-'*70}")
                for model_id in models_in_category:
                    print(f"  • {model_id}")
                print()

        # 显示推荐配置
        print(f"\n{'='*70}")
        print("💡 推荐配置示例")
        print(f"{'='*70}\n")

        # 找出最强的模型
        recommended = None
        for category in category_order:
            if category in categories and categories[category]:
                recommended = categories[category][0]
                break

        if recommended:
            print(f"bot_type: openai")
            print(f"model: \"{recommended}\"")
            print(f"provider_config:")
            print(f"  api_key: \"YOUR_API_KEY\"")
            print(f"  base_url: \"{base_url}\"")

        print()

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(
        description="列出 OpenAI 兼容 API 的可用模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从配置文件读取
  python list_openai_models.py

  # 指定 API 端点
  python list_openai_models.py --base-url https://api.openai.com/v1 --api-key sk-xxx

  # 测试第三方服务
  python list_openai_models.py --base-url https://your-api-endpoint.com/v1 --api-key your-key
        """
    )

    parser.add_argument(
        "--base-url",
        help="API 基础 URL (例如: https://api.openai.com/v1)"
    )
    parser.add_argument(
        "--api-key",
        help="API Key"
    )

    args = parser.parse_args()

    # 如果没有提供参数，尝试从配置文件读取
    if not args.base_url or not args.api_key:
        try:
            import yaml
            with open("config/bots.yaml", "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                bots = config.get("bots", {})

                for bot_key, bot_config in bots.items():
                    if bot_config.get("bot_type") == "openai":
                        provider_config = bot_config.get("provider_config", {})
                        api_key = provider_config.get("api_key")
                        base_url = provider_config.get("base_url", "https://api.openai.com/v1")

                        if api_key and not api_key.startswith("YOUR_"):
                            print(f"从配置文件读取 (bot: {bot_key})")
                            list_models(base_url, api_key)
                            return

                print("❌ 配置文件中未找到有效的 OpenAI 配置")
                print("\n使用方法:")
                print("  python list_openai_models.py --base-url URL --api-key KEY")

        except FileNotFoundError:
            print("❌ 未找到配置文件 config/bots.yaml")
            print("\n使用方法:")
            print("  python list_openai_models.py --base-url URL --api-key KEY")
        except Exception as e:
            print(f"❌ 读取配置文件失败: {e}")
    else:
        list_models(args.base_url, args.api_key)


if __name__ == "__main__":
    main()
