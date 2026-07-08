"""
B端智能售前方案生成系统 - 配置文件
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent

# 数据目录
DATA_DIR = BASE_DIR / "data"
KNOWLEDGE_BASE_DIR = DATA_DIR / "knowledge_base"
COMPETITOR_DB_DIR = DATA_DIR / "competitor_db"

# Prompt模板目录
PROMPTS_DIR = BASE_DIR / "prompts"

# LLM配置（从环境变量读取，支持多模型切换）
LLM_CONFIG = {
    "provider": os.getenv("LLM_PROVIDER", "deepseek"),  # openai / spark / deepseek
    "api_key": os.getenv("LLM_API_KEY", ""),
    "api_base": os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1"),
    "model": os.getenv("LLM_MODEL", "deepseek-chat"),
    "temperature": 0.3,
    "max_tokens": 4096,
}

# 讯飞星火特有配置
SPARK_CONFIG = {
    "app_id": os.getenv("SPARK_APP_ID", ""),
    "api_secret": os.getenv("SPARK_API_SECRET", ""),
    "api_key": os.getenv("SPARK_API_KEY", ""),
    "domain": os.getenv("SPARK_DOMAIN", "generalv4"),  # 对应模型版本
}

# 讯飞语音听写（流式版）配置
XFYUN_ASR_CONFIG = {
    "app_id": os.getenv("XFYUN_ASR_APP_ID", ""),
    "api_key": os.getenv("XFYUN_ASR_API_KEY", ""),
    "api_secret": os.getenv("XFYUN_ASR_API_SECRET", ""),
}

# 讯飞录音文件转写（长音频）配置
XFYUN_LFASR_CONFIG = {
    "app_id": os.getenv("XFYUN_LFASR_APP_ID", ""),
    "secret_key": os.getenv("XFYUN_LFASR_SECRET_KEY", ""),
}

# RAG配置
RAG_CONFIG = {
    "vector_db_path": DATA_DIR / "vector_db",
    "embedding_model": "text-embedding-3-small",  # 可替换为国产模型
    "chunk_size": 500,
    "chunk_overlap": 50,
    "top_k": 5,
}

# 行业配置（支持多行业切换）
INDUSTRY_CONFIG = {
    "current": os.getenv("INDUSTRY", "education"),
    "available": ["education", "healthcare", "manufacturing", "finance"],
}

# 输出配置
OUTPUT_CONFIG = {
    "format": "markdown",  # markdown / pdf / json
    "output_dir": BASE_DIR / "output",
}


def get_knowledge_base_path(industry: str = None) -> Path:
    """获取当前行业的知识库路径"""
    industry = industry or INDUSTRY_CONFIG["current"]
    return KNOWLEDGE_BASE_DIR / industry


def get_competitor_db_path() -> Path:
    """获取竞品数据库路径"""
    industry = INDUSTRY_CONFIG["current"]
    return COMPETITOR_DB_DIR / f"{industry}_competitors.json"


def load_prompt_template(template_name: str) -> str:
    """加载Prompt模板"""
    prompt_path = PROMPTS_DIR / f"{template_name}.md"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt template not found: {prompt_path}")
