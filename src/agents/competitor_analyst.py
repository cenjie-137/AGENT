"""
竞品智能分析模块
基于客户需求和行业竞品数据库，输出竞品对比与投标策略
"""
import json
from typing import Dict

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_client import LLMClient
from config import load_prompt_template, get_competitor_db_path


class CompetitorAnalyst:
    """竞品分析师"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.prompt_template = load_prompt_template("competitor_analyst")
        self.competitor_db = self._load_competitor_db()

    def _load_competitor_db(self) -> list:
        """加载竞品数据库"""
        db_path = get_competitor_db_path()
        if not db_path.exists():
            return []
        with open(db_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def analyze(
        self,
        customer_input: str,
        our_solution: Dict,
        industry_keywords: str = "教育信息化"
    ) -> str:
        """
        执行竞品分析

        Args:
            customer_input: 客户原始需求（用于推断竞品范围）
            our_solution: ProductMatcher输出的产品匹配结果
            industry_keywords: 行业关键词，用于聚焦竞品范围

        Returns:
            Markdown格式的竞品分析报告
        """
        our_solution_json = json.dumps(our_solution, ensure_ascii=False, indent=2)
        competitor_json = json.dumps(self.competitor_db, ensure_ascii=False, indent=2)

        prompt = (
            self.prompt_template
            .replace("{{customer_input}}", customer_input)
            .replace("{{industry_keywords}}", industry_keywords)
            .replace("{{our_solution}}", our_solution_json)
            .replace("{{competitor_db}}", competitor_json)
        )

        messages = [
            {"role": "system", "content": "你是B端市场研究专家，擅长竞品分析和差异化定位。请输出Markdown格式报告。"},
            {"role": "user", "content": prompt}
        ]

        response = self.llm.chat_completion(
            messages=messages,
            temperature=0.3,
        )

        return response.strip()

    def reload_db(self):
        """切换行业时重新加载竞品数据库"""
        self.competitor_db = self._load_competitor_db()
