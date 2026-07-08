"""
B端智能售前方案生成系统 - 主入口

用法:
    python src/main.py --input "客户需求描述"
    python src/main.py --input "客户需求描述" --customer "客户名称"
    python src/main.py --input-file customer_requirement.txt

环境变量配置 (.env 示例):
    LLM_PROVIDER=openai
    LLM_API_KEY=sk-xxxxxxxx
    LLM_MODEL=gpt-4o-mini
"""
import argparse
import json
import os
import sys
from pathlib import Path

# 确保项目根目录在路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import LLM_CONFIG, OUTPUT_CONFIG
from src.core.llm_client import LLMClient
from src.agents import IntentParser, ProductMatcher, CompetitorAnalyst, ProposalGenerator


class PresalesAgent:
    """
    B端智能售前方案生成Agent

    流程: 需求解析 -> 产品匹配 -> 竞品分析 -> 方案生成
    """

    def __init__(self, llm_config: dict = None):
        self.llm_config = llm_config or LLM_CONFIG
        self.llm_client = LLMClient(self.llm_config)

        # 初始化各模块
        self.intent_parser = IntentParser(self.llm_client)
        self.product_matcher = ProductMatcher(self.llm_client)
        self.competitor_analyst = CompetitorAnalyst(self.llm_client)
        self.proposal_generator = ProposalGenerator(self.llm_client)

    def run(self, customer_input: str, customer_name: str = "客户", save: bool = True) -> dict:
        """
        执行完整售前流程

        Returns:
            {
                "intent_result": dict,
                "matching_result": dict,
                "competitor_analysis": str,
                "proposal": str,
                "proposal_path": str (if save=True)
            }
        """
        print("=" * 60)
        print("B端智能售前方案生成系统")
        print("=" * 60)

        # Step 1: 需求解析
        print("\n[Step 1/4] 正在解析客户需求...")
        intent_result = self.intent_parser.parse(customer_input)
        print(f"  -> 识别到 {len(intent_result.get('requirements', {}).get('functional', []))} 个功能需求")
        print(f"  -> 推断出 {len(intent_result.get('implicit_needs', []))} 个隐性需求")
        print(f"  -> 发现 {len(intent_result.get('uncertainties', []))} 个待澄清问题")

        # Step 2: 产品匹配
        print("\n[Step 2/4] 正在匹配产品方案...")
        matching_result = self.product_matcher.match(intent_result)
        primary = matching_result.get("solution_combination", {}).get("primary", [])
        print(f"  -> 主方案产品数: {len(primary)}")
        print(f"  -> 方案覆盖度: {matching_result.get('gap_analysis', {}).get('standard_coverage', '未知')}")

        # Step 3: 竞品分析
        print("\n[Step 3/4] 正在分析竞品格局...")
        competitor_analysis = self.competitor_analyst.analyze(
            customer_input=customer_input,
            our_solution=matching_result
        )
        print("  -> 竞品分析完成")

        # Step 4: 方案生成
        print("\n[Step 4/4] 正在生成售前方案...")
        proposal = self.proposal_generator.generate(
            intent_result=intent_result,
            matching_result=matching_result,
            competitor_analysis=competitor_analysis,
            customer_name=customer_name
        )
        print("  -> 方案生成完成")

        # 保存结果
        result = {
            "intent_result": intent_result,
            "matching_result": matching_result,
            "competitor_analysis": competitor_analysis,
            "proposal": proposal,
            "proposal_path": None
        }

        if save:
            # 保存完整方案
            proposal_path = self.proposal_generator.save_proposal(
                proposal, filename=f"{customer_name}_售前方案.md"
            )
            result["proposal_path"] = proposal_path
            print(f"\n[完成] 方案已保存: {proposal_path}")

            # 同时保存中间结果便于调试
            debug_path = OUTPUT_CONFIG["output_dir"] / f"{customer_name}_中间结果.json"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump({
                    "intent_result": intent_result,
                    "matching_result": matching_result,
                }, f, ensure_ascii=False, indent=2)

        print("=" * 60)
        return result


def main():
    parser = argparse.ArgumentParser(description="B端智能售前方案生成系统")
    parser.add_argument("--input", "-i", type=str, help="客户原始需求描述（直接输入）")
    parser.add_argument("--input-file", "-f", type=str, help="从文件读取客户需求")
    parser.add_argument("--customer", "-c", type=str, default="客户", help="客户名称")
    parser.add_argument("--no-save", action="store_true", help="不保存输出文件")

    args = parser.parse_args()

    # 获取输入
    if args.input_file:
        input_path = Path(args.input_file)
        if not input_path.exists():
            print(f"错误: 输入文件不存在: {input_path}")
            sys.exit(1)
        customer_input = input_path.read_text(encoding="utf-8").strip()
    elif args.input:
        customer_input = args.input.strip()
    else:
        # 交互模式
        print("请输入客户需求描述（输入空行结束）:")
        lines = []
        while True:
            try:
                line = input()
                if line.strip() == "":
                    break
                lines.append(line)
            except EOFError:
                break
        customer_input = "\n".join(lines).strip()

    if not customer_input:
        print("错误: 客户需求不能为空")
        sys.exit(1)

    # 检查API Key
    if not LLM_CONFIG.get("api_key"):
        print("错误: 未配置LLM API Key")
        print("请设置环境变量 LLM_API_KEY，或在 .env 文件中配置")
        print("示例:")
        print("  Windows PowerShell: $env:LLM_API_KEY='sk-xxxx'")
        print("  Windows CMD: set LLM_API_KEY=sk-xxxx")
        sys.exit(1)

    # 运行
    agent = PresalesAgent()
    result = agent.run(
        customer_input=customer_input,
        customer_name=args.customer,
        save=not args.no_save
    )

    # 打印方案摘要
    print("\n" + "=" * 60)
    print("方案摘要预览")
    print("=" * 60)
    proposal_lines = result["proposal"].split("\n")
    # 输出前30行作为预览
    for line in proposal_lines[:30]:
        print(line)
    if len(proposal_lines) > 30:
        print(f"\n... (共 {len(proposal_lines)} 行，完整内容见输出文件)")


if __name__ == "__main__":
    main()
