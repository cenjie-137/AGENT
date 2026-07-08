"""
产品能力匹配模块
根据结构化需求，从产品知识库中匹配最优产品组合
"""
import json
from typing import Dict, List
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_client import LLMClient
from config import load_prompt_template, get_knowledge_base_path
from utils.json_extractor import safe_json_loads


class ProductMatcher:
    """产品匹配器"""

    def __init__(self, llm_client: LLMClient, industry: str = None):
        self.llm = llm_client
        self.prompt_template = load_prompt_template("product_matcher")
        self.product_catalog = self._load_product_catalog(industry)

    def _load_product_catalog(self, industry: str = None) -> List[Dict]:
        """加载产品知识库"""
        kb_path = get_knowledge_base_path(industry)
        products_file = kb_path / "products.json"

        if not products_file.exists():
            raise FileNotFoundError(f"产品知识库不存在: {products_file}")

        with open(products_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def match(self, intent_result: Dict) -> Dict:
        """
        根据需求解析结果匹配产品

        Args:
            intent_result: IntentParser输出的结构化需求

        Returns:
            产品匹配分析报告 (dict)
        """
        # 准备输入数据
        intent_json = json.dumps(intent_result, ensure_ascii=False, indent=2)
        catalog_json = json.dumps(self.product_catalog, ensure_ascii=False, indent=2)

        # 填充Prompt模板
        prompt = (
            self.prompt_template
            .replace("{{intent_result}}", intent_json)
            .replace("{{product_catalog}}", catalog_json)
        )

        messages = [
            {"role": "system", "content": "你是资深解决方案架构师，精通产品匹配与方案设计。请严格按JSON格式输出。"},
            {"role": "user", "content": prompt}
        ]

        response = self.llm.chat_completion(
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"}
        )

        result = safe_json_loads(response)

        # 基础校验与默认值
        if "matching_result" not in result:
            result["matching_result"] = []
        if "solution_combination" not in result:
            result["solution_combination"] = {"primary": [], "alternative": [], "total_estimate": "待评估"}
        if "gap_analysis" not in result:
            result["gap_analysis"] = {"standard_coverage": "未知", "custom_dev_needed": [], "third_party_required": []}
        if "deployment_suggestion" not in result:
            result["deployment_suggestion"] = {"mode": "待评估", "phases": []}

        return result

    def reload_catalog(self, industry: str = None):
        """切换行业时重新加载产品知识库"""
        self.product_catalog = self._load_product_catalog(industry)
