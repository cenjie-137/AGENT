# B端智能售前方案生成系统

> 一个跨行业的B端售前Agent框架，支持自然语言客户需求输入，自动输出结构化售前方案。
> 当前MVP以教育行业为示例，可通过替换知识库适配任意行业。

## 核心能力

```
客户需求(自然语言)
    |
    v
[Intent Parser]  需求智能解析 -> 结构化需求报告
    |
    v
[Product Matcher] 产品能力匹配 -> 最优产品组合 + Gap分析
    |
    v
[Competitor Analyst] 竞品智能分析 -> 对比矩阵 + 投标策略
    |
    v
[Proposal Generator] 方案智能生成 -> 完整Markdown售前方案
```

## 项目结构

```
b2b-presales-agent/
├── docs/
│   └── PRD.md                    # 产品需求文档
├── data/
│   ├── knowledge_base/
│   │   └── education/
│   │       └── products.json     # 产品知识库(可替换行业)
│   └── competitor_db/
│       └── education_competitors.json  # 竞品数据库
├── prompts/
│   ├── intent_parser.md          # 需求解析Prompt
│   ├── product_matcher.md        # 产品匹配Prompt
│   ├── competitor_analyst.md     # 竞品分析Prompt
│   └── proposal_generator.md     # 方案生成Prompt
├── src/
│   ├── config.py                 # 全局配置(多LLM/多行业)
│   ├── main.py                   # 主入口
│   ├── core/
│   │   └── llm_client.py         # 多模型抽象层(OpenAI/DeepSeek/星火)
│   ├── agents/
│   │   ├── intent_parser.py      # 需求解析模块
│   │   ├── product_matcher.py    # 产品匹配模块
│   │   ├── competitor_analyst.py # 竞品分析模块
│   │   └── proposal_generator.py # 方案生成模块
│   └── utils/
│       └── json_extractor.py     # JSON安全提取工具
├── requirements.txt
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置API Key

选择以下任一方式：

**方式A：环境变量（推荐）**

```powershell
# Windows PowerShell
$env:LLM_PROVIDER="openai"
$env:LLM_API_KEY="sk-xxxxxxxxxxxxxxxx"
$env:LLM_MODEL="gpt-4o-mini"
```

**方式B：DeepSeek（国内友好，价格便宜）**

```powershell
$env:LLM_PROVIDER="deepseek"
$env:LLM_API_KEY="sk-xxxxxxxxxxxxxxxx"
$env:LLM_API_BASE="https://api.deepseek.com/v1"
$env:LLM_MODEL="deepseek-chat"
```

### 3. 运行示例

```bash
# 方式1: 命令行直接输入
python src/main.py --input "我们是师范大学，想建设智慧教室，提升师范生教学技能。" --customer "某师范大学"

# 方式2: 从文件读取需求
python src/main.py --input-file requirement.txt --customer "某教育局"

# 方式3: 交互模式（不传入input，按提示输入）
python src/main.py --customer "某高校"
```

### 4. 查看输出

运行后会在 `output/` 目录生成：
- `{客户名}_售前方案.md` — 完整售前方案
- `{客户名}_中间结果.json` — 需求解析 & 产品匹配的中间数据（便于调试）

## 多行业适配

本项目采用**可插拔知识库**设计，切换行业只需替换数据文件：

| 步骤 | 操作 |
|------|------|
| 1 | 在 `data/knowledge_base/` 下新建行业目录，如 `healthcare/` |
| 2 | 放入该行业的 `products.json`（结构参考education目录） |
| 3 | 在 `data/competitor_db/` 下放入 `{industry}_competitors.json` |
| 4 | 修改Prompt模板中的行业描述（或直接运行，LLM会自适应） |
| 5 | 运行前设置环境变量: `$env:INDUSTRY="healthcare"` |

**核心Pipeline保持不变**：4个Agent模块、LLM调用逻辑、配置系统全部复用。

## 技术栈

- **Python 3.10+** — 核心语言
- **OpenAI API / DeepSeek API** — LLM调用（兼容格式）
- **Prompt Engineering** — 分层Prompt架构（系统/任务/示例/校验）
- **JSON结构化输出** — 模块间数据契约
- **模块化设计** — 各Agent可独立调用，也可Pipeline串联

## 后续扩展路线

| 阶段 | 内容 | 状态 |
|------|------|------|
| Week 1 | Python MVP框架 + Prompt工程 | 已完成 |
| Week 2 | 接入向量检索(RAG) + Gradio界面 | 待开发 |
| Week 3 | 需求解析增强 + 知识库自动构建 | 待开发 |
| Week 4 | 竞品分析模块完善 + 报告导出PDF | 待开发 |
| Week 5 | 多行业适配验证 + 自动化测试 | 待开发 |
| Week 6 | GitHub上线 + README完善 + 演示视频 | 待开发 |

## License

MIT
