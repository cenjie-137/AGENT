"""
JSON提取工具
处理LLM返回的Markdown包裹JSON
"""
import json
import re


def extract_json_from_markdown(text: str) -> str:
    """从Markdown代码块中提取JSON字符串"""
    if not text:
        return "{}"

    # 尝试匹配 ```json ... ```
    pattern = r"```(?:json)?\s*([\s\S]*?)```"
    matches = re.findall(pattern, text, re.IGNORECASE)
    if matches:
        return matches[-1].strip()

    # 尝试匹配 ``` ... ``` (无语言标记)
    pattern2 = r"```\s*([\s\S]*?)```"
    matches2 = re.findall(pattern2, text)
    if matches2:
        return matches2[-1].strip()

    # 尝试从文本中提取最外层的花括号内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1].strip()

    # 尝试提取方括号JSON数组
    start_arr = text.find("[")
    end_arr = text.rfind("]")
    if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
        return text[start_arr:end_arr + 1].strip()

    return text.strip()


def safe_json_loads(text: str) -> dict | list:
    """安全解析JSON，支持Markdown包裹"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = extract_json_from_markdown(text)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法解析JSON: {e}\n原始文本:\n{text[:500]}")
