"""
方案智能生成模块
综合前三步结果，输出完整的售前方案文档
"""
import json
from typing import Dict

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_client import LLMClient
from config import load_prompt_template, OUTPUT_CONFIG


class ProposalGenerator:
    """售前方案生成器"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.prompt_template = load_prompt_template("proposal_generator")

    def generate(
        self,
        intent_result: Dict,
        matching_result: Dict,
        competitor_analysis: str,
        customer_name: str = "客户"
    ) -> str:
        """
        生成完整售前方案

        Args:
            intent_result: 需求解析结果
            matching_result: 产品匹配结果
            competitor_analysis: 竞品分析报告 (Markdown文本)
            customer_name: 客户名称，用于方案标题

        Returns:
            Markdown格式的完整售前方案
        """
        intent_json = json.dumps(intent_result, ensure_ascii=False, indent=2)
        matching_json = json.dumps(matching_result, ensure_ascii=False, indent=2)

        # 注入当前日期，确保时间线从今天开始计算
        from datetime import datetime
        today_str = datetime.now().strftime("%Y年%m月%d日")

        prompt = (
            self.prompt_template
            .replace("{{intent_result}}", intent_json)
            .replace("{{matching_result}}", matching_json)
            .replace("{{competitor_analysis}}", competitor_analysis)
            .replace("{{客户名称}}", customer_name)
            .replace("{{当前日期}}", today_str)
        )

        messages = [
            {"role": "system", "content": "你是资深解决方案架构师，擅长将产品能力转化为客户价值的方案文档。请输出专业Markdown格式。"},
            {"role": "user", "content": prompt}
        ]

        response = self.llm.chat_completion(
            messages=messages,
            temperature=0.4,
            max_tokens=4096,
        )

        return response.strip()

    def save_proposal(self, content: str, filename: str = None) -> str:
        """
        保存方案到输出目录

        Returns:
            保存的文件路径
        """
        output_dir = OUTPUT_CONFIG["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            from datetime import datetime
            filename = f"proposal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

        file_path = output_dir / filename
        file_path.write_text(content, encoding="utf-8")
        return str(file_path)
