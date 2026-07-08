# B端智能售前方案生成系统

> 跨行业B端售前Agent系统，支持自然语言需求输入、**语音实时录入**、**会议录音转写与需求提炼**，自动输出结构化售前方案并生成客户确认问卷。
> 覆盖教育、医疗、制造、金融四大行业，通过替换知识库可适配任意行业。

## 核心功能

### 多种需求输入方式

| 方式 | 说明 | 适用场景 |
|------|------|---------|
| **文本输入** | 直接输入客户需求描述 | 常规需求录入 |
| **实时录音** | 浏览器麦克风录音，自动转为文字 | 快速口述需求（1分钟以内） |
| **上传音频** | 上传MP3/WAV等格式的录音文件 | 会议录音转写（最长5小时，支持MP3） |
| **AI需求提炼** | 转写文本由大模型自动提取客户名称、行业、需求描述 | 语音输入后一键填充表单 |

### 核心Pipeline

```
客户需求(语音/文本)
    |
    ├── [语音转写] 讯飞ASR实时转写 / 录音文件转写 (最长5h)
    |
    v
[需求解析]  →  结构化需求报告（痛点/场景/关键因素）
    |
    v
[产品匹配]  →  最优产品组合 + Gap分析
    |
    v
[竞品分析]  →  对比矩阵 + 投标策略
    |
    v
[方案生成]  →  完整售前方案（含实施路径/ROI/风险应对）
    |
    v
[问卷确认]  →  客户确认事项弹窗（含附件上传+补充说明）
```

### 其他特性

- **实时进度展示**：SSE流式推送，生成过程实时可见
- **多行业知识库**：教育/医疗/制造/金融四行业产品库+竞品库
- **数据可视化看板**：需求分类、产品匹配评分、竞品对比等ECharts图表
- **方案导出**：支持PDF导出（A4打印格式）和Markdown下载
- **历史记录管理**：方案保存、查看、批量删除
- **状态持久化**：浏览其他页面后返回，内容自动恢复

## 项目结构

```
b2b-presales-agent/
├── src/
│   ├── web_app.py                 # Flask主应用 + 前端HTML/CSS/JS
│   ├── config.py                  # 全局配置(多LLM/多行业/讯飞ASR)
│   ├── main.py                    # 命令行入口
│   ├── core/
│   │   ├── llm_client.py          # 多模型抽象层(OpenAI/DeepSeek/星火)
│   │   ├── xfyun_asr.py           # 讯飞语音听写客户端(实时短音频)
│   │   └── xfyun_lfasr.py         # 讯飞录音文件转写客户端(长音频MP3)
│   ├── agents/
│   │   ├── intent_parser.py       # 需求解析模块
│   │   ├── product_matcher.py     # 产品匹配模块
│   │   ├── competitor_analyst.py  # 竞品分析模块
│   │   └── proposal_generator.py  # 方案生成模块
│   └── utils/
│       └── json_extractor.py      # JSON安全提取工具
├── data/
│   ├── knowledge_base/
│   │   ├── education/             # 教育行业产品库(8款产品)
│   │   ├── healthcare/            # 医疗行业产品库(6款)
│   │   ├── manufacturing/         # 制造行业产品库(6款)
│   │   └── finance/               # 金融行业产品库(6款)
│   └── competitor_db/             # 四行业竞品数据库
├── prompts/                       # 分层Prompt模板
│   ├── intent_parser.md
│   ├── product_matcher.md
│   ├── competitor_analyst.md
│   └── proposal_generator.md
├── .env.example                   # 环境变量模板
├── requirements.txt
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填入密钥：

```powershell
# Windows PowerShell
$env:LLM_API_KEY="sk-xxxxxxxxxxxxxxxx"      # DeepSeek/OpenAI密钥
$env:LLM_PROVIDER="deepseek"                # deepseek / openai / spark
$env:XFYUN_ASR_APP_ID="5de6e957"           # 讯飞语音听写(选配)
$env:XFYUN_ASR_API_KEY="xxx"
$env:XFYUN_ASR_API_SECRET="xxx"
$env:XFYUN_LFASR_SECRET_KEY="xxx"          # 讯飞录音转写(选配)
```

### 3. 启动Web服务

```bash
cd b2b-presales-agent
python -m flask --app src/web_app.py run --host=0.0.0.0 --port=5000
```

访问 [http://localhost:5000](http://localhost:5000)，登录后即可使用。

### 4. 命令行模式（无需Web界面）

```bash
python src/main.py --input "我们是师范大学，想建设智慧教室，提升师范生教学技能。" --customer "某师范大学"
```

## 多行业适配

切换行业只需设置环境变量同时替换知识库数据：

| 步骤 | 操作 |
|------|------|
| 1 | 在 `data/knowledge_base/` 下新建行业目录 |
| 2 | 放入该行业的 `products.json` |
| 3 | 在 `data/competitor_db/` 下放入 `{industry}_competitors.json` |
| 4 | 运行前设置: `$env:INDUSTRY="healthcare"` |

## 技术栈

- **Python 3.10+ / Flask** — 后端框架
- **DeepSeek / OpenAI API** — LLM调用
- **讯飞语音听写(WebSocket)** — 实时语音转写（1分钟内）
- **讯飞录音文件转写(REST)** — 长音频MP3转写（最长5小时）
- **ECharts** — 数据可视化看板
- **Web Audio API** — 浏览器端音频录制
- **Prompt Engineering** — 分层Prompt架构
- **SSE流式推送** — 实时进度展示

## License

MIT
