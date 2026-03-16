#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
列出可用的 Gemini 模型

使用方法：
    python list_gemini_models.py YOUR_API_KEY
"""

import sys
import json
import requests

# Windows 控制台编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


def list_models(api_key, api_version="v1"):
    """列出指定 API 版本的可用模型"""
    url = f"https://generativelanguage.googleapis.com/{api_version}/models?key={api_key}"

    print(f"\n{'='*60}")
    print(f"正在查询 {api_version} API 的可用模型...")
    print(f"{'='*60}\n")

    try:
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            print(f"❌ 错误: HTTP {response.status_code}")
            print(response.text[:500])
            return []

        data = response.json()
        models = data.get("models", [])

        if not models:
            print("⚠️  未找到任何模型")
            return []

        print(f"✅ 找到 {len(models)} 个模型:\n")

        available_models = []
        for model in models:
            name = model.get("name", "")
            display_name = model.get("displayName", "")
            description = model.get("description", "")
            supported_methods = model.get("supportedGenerationMethods", [])

            # 提取模型 ID（去掉 models/ 前缀）
            model_id = name.replace("models/", "")

            # 检查是否支持 generateContent
            supports_generate = "generateContent" in supported_methods

            if supports_generate:
                available_models.append(model_id)
                print(f"📦 {model_id}")
                if display_name:
                    print(f"   名称: {display_name}")
                if description:
                    desc_short = description[:80] + "..." if len(description) > 80 else description
                    print(f"   描述: {desc_short}")
                print(f"   支持方法: {', '.join(supported_methods)}")
                print()

        return available_models

    except requests.exceptions.RequestException as e:
        print(f"❌ 网络错误: {e}")
        return []


def main():
    if len(sys.argv) < 2:
        print("使用方法: python list_gemini_models.py YOUR_API_KEY")
        print("\n或者从配置文件读取:")

        # 尝试从配置文件读取
        try:
            import yaml
            with open("config/bots.yaml", "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                bots = config.get("bots", {})
                for bot_key, bot_config in bots.items():
                    if bot_config.get("bot_type") == "gemini":
                        api_key = bot_config.get("provider_config", {}).get("api_key")
                        if api_key and not api_key.startswith("YOUR_"):
                            print(f"\n从配置文件读取到 API Key (bot: {bot_key})")
                            break
                else:
                    print("\n❌ 配置文件中未找到有效的 Gemini API Key")
                    sys.exit(1)
        except Exception as e:
            print(f"\n❌ 读取配置文件失败: {e}")
            sys.exit(1)
    else:
        api_key = sys.argv[1]

    # 查询 v1 API
    v1_models = list_models(api_key, "v1")

    # 查询 v1beta API
    v1beta_models = list_models(api_key, "v1beta")

    # 总结
    print(f"\n{'='*60}")
    print("📊 总结")
    print(f"{'='*60}\n")

    if v1_models:
        print(f"✅ v1 API 可用模型 ({len(v1_models)} 个):")
        for model in v1_models:
            print(f"   - {model}")
        print()

    if v1beta_models:
        print(f"✅ v1beta API 可用模型 ({len(v1beta_models)} 个):")
        for model in v1beta_models:
            print(f"   - {model}")
        print()

    # 推荐配置
    if v1_models or v1beta_models:
        print(f"{'='*60}")
        print("💡 推荐配置")
        print(f"{'='*60}\n")

        if v1_models:
            recommended = v1_models[0]
            print(f"在 config/bots.yaml 中使用:")
            print(f"""
  gemini_bot:
    bot_type: "gemini"
    model: "{recommended}"
    provider_config:
      api_key: "YOUR_API_KEY"
""")


if __name__ == "__main__":
    main()
