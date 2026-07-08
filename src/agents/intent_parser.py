"""
需求智能解析模块
将客户的自然语言需求转化为结构化需求报告
"""
import json
from typing import Dict

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_client import LLMClient
from config import load_prompt_template
from utils.json_extractor import safe_json_loads


class IntentParser:
    """客户需求意图解析器"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.prompt_template = load_prompt_template("intent_parser")

    def parse(self, customer_input: str) -> Dict:
        """
        解析客户需求

        Args:
            customer_input: 客户的原始需求描述

        Returns:
            结构化需求解析报告 (dict)
        """
        if not customer_input or not customer_input.strip():
            raise ValueError("客户输入不能为空")

        # 填充Prompt模板
        prompt = self.prompt_template.replace("{{customer_input}}", customer_input.strip())

        messages = [
            {"role": "system", "content": "你是一位资深B端售前顾问，擅长从客户模糊描述中提取结构化需求。请严格按JSON格式输出。"},
            {"role": "user", "content": prompt}
        ]

        # 调用LLM，要求JSON输出
        response = self.llm.chat_completion(
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"}
        )

        # 解析JSON结果
        result = safe_json_loads(response)

        # 基础校验
        if "customer_profile" not in result:
            result["customer_profile"] = {}
        if "requirements" not in result:
            result["requirements"] = {"functional": [], "non_functional": []}
        if "implicit_needs" not in result:
            result["implicit_needs"] = []
        if "uncertainties" not in result:
            result["uncertainties"] = []

        return result

    def batch_parse(self, inputs: list[str]) -> list[Dict]:
        """批量解析多个客户需求"""
        return [self.parse(inp) for inp in inputs]
