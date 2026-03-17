"""
文本处理工具模块

提供文本清理、格式化等实用函数。
"""

import re
import logging

logger = logging.getLogger(__name__)


def clean_think_tags(text: str) -> str:
    """清理<think>标签内的markdown格式

    从<think></think>标签内的内容中移除markdown表格、加粗等格式，
    只保留纯文本和emoji。这确保企业微信机器人显示的思考过程简洁清晰。

    Args:
        text: 包含<think>标签的文本

    Returns:
        str: 清理后的文本

    Example:
        >>> text = '<think>\\n| 步骤 | 状态 |\\n|------|------|\\n| 查询 | **完成** |\\n</think>\\n结果'
        >>> clean_think_tags(text)
        '<think>\\n查询\\n</think>\\n结果'
    """
    def clean_content(match):
        """清理<think>标签内的内容"""
        opening_tag = match.group(1)  # <think> 或 <think ...>
        content = match.group(2)

        # 分行处理
        lines = content.split('\n')
        cleaned_lines = []

        for line in lines:
            # 去除表格行（包含 | 符号的行）
            if '|' in line:
                # 检查是否是表格行
                # 表格行通常格式: | xxx | yyy | 或 |------|------|
                stripped = line.strip()
                if stripped.startswith('|') or '|' in stripped:
                    # 尝试提取表格单元格中的纯文本内容
                    # 但如果是分隔行（全是-和|），则跳过
                    if re.match(r'^[\|\-\s]+$', stripped):
                        # 分隔行，跳过
                        continue
                    else:
                        # 数据行，提取文本（去掉 | 和 **）
                        cells = [cell.strip() for cell in stripped.split('|') if cell.strip()]
                        if cells:
                            # 只保留第一个单元格的内容作为文本描述
                            cell_text = cells[0]
                            # 去除加粗
                            cell_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', cell_text)
                            cleaned_lines.append(cell_text)
                        continue

            # 去除加粗标记（**text**）
            line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)

            # 去除HTML标签（如<font color="xxx">）
            line = re.sub(r'<[^>]+>', '', line)

            # 保留空行和有内容的行
            if line or not line.strip():
                cleaned_lines.append(line)

        # 重新组合
        cleaned_content = '\n'.join(cleaned_lines)

        # 去除多余的连续空行，最多保留一个空行
        cleaned_content = re.sub(r'\n{3,}', '\n\n', cleaned_content)

        return f"{opening_tag}{cleaned_content}</think>"

    # 匹配所有<think>...</think>标签（支持<think>或<think ...>的变体）
    # 使用非贪婪匹配，支持多个<think>标签
    pattern = r'(<think[^>]*>)(.*?)(</think>)'

    result = re.sub(pattern, clean_content, text, flags=re.DOTALL)

    # 如果进行了清理，记录日志
    if result != text:
        logger.info("[文本清理] 已清理<think>标签内的markdown格式")
        logger.debug(f"[文本清理] 原始长度: {len(text)}, 清理后长度: {len(result)}")

    return result


def remove_think_tags(text: str) -> str:
    """完全移除<think>标签及其内容

    从文本中完全移除<think></think>标签及其包含的内容。
    适用于不需要显示思考过程的场景。

    Args:
        text: 包含<think>标签的文本

    Returns:
        str: 移除<think>标签后的文本

    Example:
        >>> text = '这是结果<think>思考过程</think>更多内容'
        >>> remove_think_tags(text)
        '这是结果更多内容'
    """
    # 移除所有<think>...</think>标签及其内容
    pattern = r'<think[^>]*>.*?</think>'
    result = re.sub(pattern, '', text, flags=re.DOTALL)

    # 清理可能产生的多余空白
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = result.strip()

    if result != text:
        logger.info("[文本清理] 已移除<think>标签及其内容")

    return result
