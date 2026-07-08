"""
B端智能售前方案生成系统 - Flask Web界面 (v3.2)
新增: SSE百分比进度条 / 问卷补充更新 / PRD格式PDF导出
"""
import sys, os, json, traceback, hashlib, secrets, re, uuid, threading
from pathlib import Path
from datetime import datetime
from functools import wraps

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["no_proxy"] = "localhost,127.0.0.1"

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, flash, Response, stream_with_context
from src.config import LLM_CONFIG, get_knowledge_base_path, XFYUN_ASR_CONFIG, XFYUN_LFASR_CONFIG
from src.core.llm_client import LLMClient
from src.core.xfyun_asr import XfyunASRClient, convert_audio_to_pcm
from src.core.xfyun_lfasr import XfyunLFASRClient
from src.agents import IntentParser, ProductMatcher, CompetitorAnalyst, ProposalGenerator
from src.utils.json_extractor import safe_json_loads

app = Flask(__name__, static_folder=str(PROJECT_ROOT / "static"), static_url_path="/static")
app.secret_key = secrets.token_hex(32)

# ==================== 数据存储 ====================
DATA_DIR = PROJECT_ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"
HISTORY_DIR = DATA_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 录音文件转写任务存储 ====================
_lfasr_tasks = {}
_lfasr_lock = threading.Lock()


def _load_users():
    if USERS_FILE.exists():
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def _load_history(username):
    hf = HISTORY_DIR / f"{username}_history.json"
    if hf.exists():
        with open(hf, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_history(username, records):
    hf = HISTORY_DIR / f"{username}_history.json"
    with open(hf, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


# 预置默认用户
if not USERS_FILE.exists():
    _save_users({"admin": {"password": _hash_password("admin123"), "created_at": datetime.now().isoformat()}})


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ==================== Markdown 转 HTML (服务端) ====================

def md_to_html(md_text):
    """完整的Markdown转HTML"""
    if not md_text:
        return ""

    lines = md_text.split('\n')
    html_lines = []
    in_table = False
    table_rows = []
    in_code = False
    code_lines = []
    in_list = False
    list_type = None
    list_items = []

    def flush_table():
        nonlocal in_table, table_rows
        if not table_rows:
            in_table = False
            return
        # 过滤分隔行
        data_rows = []
        for row in table_rows:
            cells = [c.strip() for c in row.split('|')]
            cells = [c for c in cells if c]
            if cells and all(set(c) <= set('|-: \t') for c in cells):
                continue  # 分隔行
            if cells:
                data_rows.append(cells)
        if data_rows:
            thead = '<tr>' + ''.join(f'<th>{parse_inline(c)}</th>' for c in data_rows[0]) + '</tr>'
            tbody = ''
            for row in data_rows[1:]:
                tbody += '<tr>' + ''.join(f'<td>{parse_inline(c)}</td>' for c in row) + '</tr>'
            html_lines.append(f'<table><thead>{thead}</thead><tbody>{tbody}</tbody></table>')
        in_table = False
        table_rows = []

    def flush_list():
        nonlocal in_list, list_items, list_type
        if list_items:
            tag = 'ol' if list_type == 'ol' else 'ul'
            html_lines.append(f'<{tag}>' + ''.join(list_items) + f'</{tag}>')
        in_list = False
        list_items = []
        list_type = None

    def flush_code():
        nonlocal in_code, code_lines
        if code_lines:
            content = '\n'.join(code_lines)
            html_lines.append(f'<pre><code>{html_escape(content)}</code></pre>')
        in_code = False
        code_lines = []

    for line in lines:
        stripped = line.strip()

        # 代码块
        if stripped.startswith('```'):
            if in_code:
                flush_code()
            else:
                flush_table()
                flush_list()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        # 空行
        if not stripped:
            flush_table()
            flush_list()
            continue

        # 表格行
        if stripped.startswith('|') and '|' in stripped[1:]:
            flush_list()
            if not in_table:
                in_table = True
            table_rows.append(stripped)
            continue
        else:
            flush_table()

        # 标题
        if stripped.startswith('###### '):
            flush_list()
            html_lines.append(f'<h6>{parse_inline(stripped[7:])}</h6>')
            continue
        if stripped.startswith('##### '):
            flush_list()
            html_lines.append(f'<h5>{parse_inline(stripped[6:])}</h5>')
            continue
        if stripped.startswith('#### '):
            flush_list()
            html_lines.append(f'<h4>{parse_inline(stripped[5:])}</h4>')
            continue
        if stripped.startswith('### '):
            flush_list()
            html_lines.append(f'<h3>{parse_inline(stripped[4:])}</h3>')
            continue
        if stripped.startswith('## '):
            flush_list()
            html_lines.append(f'<h2>{parse_inline(stripped[3:])}</h2>')
            continue
        if stripped.startswith('# '):
            flush_list()
            html_lines.append(f'<h1>{parse_inline(stripped[2:])}</h1>')
            continue

        # 列表
        if stripped.startswith('- ') or stripped.startswith('* '):
            if not in_list or list_type != 'ul':
                flush_list()
                in_list = True
                list_type = 'ul'
            list_items.append(f'<li>{parse_inline(stripped[2:])}</li>')
            continue
        if re.match(r'^\d+\.\s', stripped):
            if not in_list or list_type != 'ol':
                flush_list()
                in_list = True
                list_type = 'ol'
            text = re.sub(r'^\d+\.\s', '', stripped)
            list_items.append(f'<li>{parse_inline(text)}</li>')
            continue

        flush_list()

        # 普通段落
        html_lines.append(f'<p>{parse_inline(stripped)}</p>')

    flush_table()
    flush_list()
    flush_code()

    return '<div class="markdown-body">' + '\n'.join(html_lines) + '</div>'


def html_escape(text):
    return re.sub(r'<br\s*/?>', ' ', text, flags=re.IGNORECASE).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def parse_inline(text):
    """处理行内元素: **bold**, *italic*, `code`"""
    # 先处理code避免被其他规则影响
    parts = []
    i = 0
    while i < len(text):
        # 代码
        if text[i:i+1] == '`':
            end = text.find('`', i+1)
            if end != -1:
                parts.append(f'<code>{html_escape(text[i+1:end])}</code>')
                i = end + 1
                continue
        # 粗体 **
        if text[i:i+2] == '**':
            end = text.find('**', i+2)
            if end != -1:
                parts.append(f'<strong>{parse_inline(text[i+2:end])}</strong>')
                i = end + 2
                continue
        # 斜体 * (但排除**)
        if text[i:i+1] == '*' and (i+1 >= len(text) or text[i+1] != '*'):
            end = text.find('*', i+1)
            if end != -1 and text[end:end+1] == '*' and (end+1 >= len(text) or text[end+1] != '*'):
                parts.append(f'<em>{parse_inline(text[i+1:end])}</em>')
                i = end + 1
                continue
        parts.append(html_escape(text[i:i+1]))
        i += 1
    return ''.join(parts)


# ==================== CSS & JS 公共片段 ====================

COMMON_HEAD = r"""
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
           background: #f0f2f5; color: #333; min-height: 100vh; }
    .header { background: linear-gradient(135deg, #1a73e8 0%, #0d47a1 100%);
              color: white; padding: 14px 28px; display: flex; align-items: center; justify-content: space-between; }
    .header-left { display: flex; align-items: center; gap: 10px; }
    .header h1 { font-size: 18px; font-weight: 600; }
    .header .badge { background: rgba(255,255,255,.2); border-radius: 10px; padding: 1px 8px; font-size: 10px; }
    .header-right { display: flex; align-items: center; gap: 14px; font-size: 13px; }
    .header-right a { color: rgba(255,255,255,.9); text-decoration: none; }
    .header-right a:hover { color: white; }
    .container { max-width: 1500px; margin: 16px auto; padding: 0 16px; display: flex; gap: 16px; align-items: flex-start; }
    .panel { background: white; border-radius: 10px; box-shadow: 0 1px 6px rgba(0,0,0,.06); }
    .panel-left { width: 320px; flex-shrink: 0; padding: 20px; }
    .panel-right { flex: 1; min-width: 0; }
    .panel h3 { font-size: 14px; margin-bottom: 14px; color: #1a73e8; display: flex; align-items: center; gap: 6px; }
    .form-group { margin-bottom: 12px; }
    .form-group label { display: block; font-size: 12px; font-weight: 500; margin-bottom: 4px; color: #555; }
    .form-group input, .form-group select, .form-group textarea {
        width: 100%; padding: 8px 10px; border: 1px solid #d9d9d9; border-radius: 6px;
        font-size: 13px; transition: all .2s; font-family: inherit; }
    .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
        border-color: #1a73e8; outline: none; box-shadow: 0 0 0 2px rgba(26,115,232,.1); }
    .form-group textarea { resize: vertical; min-height: 140px; }
    .btn-primary { width: 100%; padding: 10px; background: #1a73e8; color: white; border: none;
                   border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer;
                   transition: all .2s; display: flex; align-items: center; justify-content: center; gap: 6px; }
    .btn-primary:hover { background: #1557b0; }
    .btn-primary:disabled { background: #94b8e8; cursor: not-allowed; }
    .btn-secondary { display: inline-flex; align-items: center; gap: 5px; padding: 6px 12px;
                     background: #fff; color: #1a73e8; border: 1px solid #1a73e8; border-radius: 6px;
                     font-size: 12px; cursor: pointer; transition: all .2s; text-decoration: none; }
    .btn-secondary:hover { background: #e8f0fe; }
    .btn-group { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
    .status-box { margin-top: 12px; padding: 10px; background: #f6f8fa; border-radius: 6px;
                  font-size: 12px; line-height: 1.5; color: #666;
                  border: 1px solid #e8e8e8; }
    .status-box.error { background: #fff1f0; border-color: #ffa39e; color: #cf1322; }
    .status-box.success { background: #f6ffed; border-color: #b7eb8f; color: #389e0d; }
    .progress-bar { width: 100%; height: 8px; background: #e8e8e8; border-radius: 4px; overflow: hidden; margin-bottom: 6px; }
    .progress-fill { height: 100%; background: linear-gradient(90deg, #1a73e8, #4285f4); border-radius: 4px;
                     transition: width 0.3s ease; width: 0%; }
    .progress-text { font-size: 12px; color: #555; font-family: 'Consolas', monospace; }
    .tabs { display: flex; gap: 0; border-bottom: 2px solid #e8e8e8; background: #fafbfc; border-radius: 10px 10px 0 0; }
    .tab-btn { padding: 10px 16px; font-size: 13px; cursor: pointer; border: none;
               background: none; color: #666; position: relative; transition: all .2s; }
    .tab-btn:hover { color: #1a73e8; }
    .tab-btn.active { color: #1a73e8; font-weight: 600; background: white; }
    .tab-btn.active::after { content: ''; position: absolute; bottom: -2px; left: 6px; right: 6px;
                             height: 2px; background: #1a73e8; }
    .tab-content { display: none; padding: 16px; }
    .tab-content.active { display: block; }
    .tab-content pre { background: #f6f8fa; padding: 14px; border-radius: 6px; font-size: 12px;
                       overflow-x: auto; white-space: pre-wrap; word-break: break-word; max-height: 65vh; }
    .tips { font-size: 11px; color: #999; margin-top: 10px; line-height: 1.7; }
    .voice-section { margin: 14px 0; padding: 12px; background: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0; }
    .voice-header { font-size: 13px; font-weight: 600; color: #333; margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
    .voice-hint { font-size: 11px; color: #999; font-weight: normal; margin-left: auto; }
    .voice-controls { display: flex; gap: 8px; margin-bottom: 8px; }
    .btn-record { flex: 1; padding: 8px 12px; border: 1px solid #cf1322; background: white; color: #cf1322; border-radius: 6px; cursor: pointer; font-size: 13px; display: flex; align-items: center; justify-content: center; gap: 5px; transition: all 0.2s; }
    .btn-record:hover { background: #cf1322; color: white; }
    .btn-record.recording { background: #cf1322; color: white; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }
    .btn-upload { flex: 1; padding: 8px 12px; border: 1px solid #1a73e8; background: white; color: #1a73e8; border-radius: 6px; cursor: pointer; font-size: 13px; display: flex; align-items: center; justify-content: center; gap: 5px; transition: all 0.2s; }
    .btn-upload:hover { background: #1a73e8; color: white; }
    .voice-status { font-size: 12px; color: #666; min-height: 18px; margin-bottom: 4px; }
    .voice-result { margin-top: 10px; padding: 10px; background: white; border-radius: 6px; border: 1px solid #e2e8f0; }
    .voice-result-label { font-size: 12px; font-weight: 600; color: #333; margin-bottom: 6px; }
    .voice-transcript { font-size: 12px; color: #444; line-height: 1.6; max-height: 120px; overflow-y: auto; background: #f6f8fa; padding: 8px; border-radius: 4px; white-space: pre-wrap; word-break: break-word; }
    .voice-actions { display: flex; gap: 8px; margin-top: 10px; }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
               border-top-color: white; border-radius: 50%; animation: spin .6s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 960px) { .container { flex-direction: column; } .panel-left { width: 100%; } }
    .placeholder-text { color: #999; text-align: center; padding: 30px; font-size: 13px; }

    /* Markdown */
    .markdown-body { line-height: 1.7; font-size: 14px; }
    .markdown-body h1 { font-size: 20px; margin: 20px 0 10px; color: #222; }
    .markdown-body h2 { font-size: 17px; margin: 16px 0 8px; color: #1a73e8; padding-bottom: 4px; border-bottom: 1px solid #eee; }
    .markdown-body h3 { font-size: 14px; margin: 12px 0 6px; color: #333; }
    .markdown-body table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 12px;
        page-break-inside: avoid; word-wrap: break-word; table-layout: auto; }
    .markdown-body th, .markdown-body td { border: 1px solid #ddd; padding: 6px 10px; text-align: left;
        word-break: break-word; overflow-wrap: break-word; vertical-align: top; white-space: normal; }
    .markdown-body th { background: #f0f4f8; font-weight: 600; }
    .markdown-body strong { color: #1a73e8; }
    .markdown-body code { background: #f0f2f5; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
    .markdown-body ul, .markdown-body ol { padding-left: 24px; margin: 6px 0; }
    .markdown-body li { margin: 3px 0; }
    .markdown-body p { margin: 6px 0; }

    /* Charts */
    .chart-row { display: flex; gap: 12px; margin: 12px 0; flex-wrap: wrap; }
    .chart-box { flex: 1; min-width: 320px; height: 300px; min-height: 300px; background: #fafbfc;
                 border-radius: 6px; border: 1px solid #eee; }
    .chart-full { width: 100%; height: 350px; min-height: 350px; margin: 12px 0; background: #fafbfc;
                  border-radius: 6px; border: 1px solid #eee; }

    /* Product cards */
    .product-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; margin: 10px 0; }
    .product-card { border: 1px solid #eee; border-radius: 8px; overflow: hidden; transition: all .2s; background: white; }
    .product-card:hover { box-shadow: 0 3px 12px rgba(0,0,0,.1); transform: translateY(-1px); }
    .product-card-icon { width: 100%; height: 90px; display: flex; align-items: center; justify-content: center;
                         font-size: 36px; color: white; }
    .product-card-body { padding: 10px; }
    .product-card-body h4 { font-size: 13px; margin-bottom: 3px; }
    .product-card-body .tag { display: inline-block; background: #e8f0fe; color: #1a73e8; font-size: 10px;
                              padding: 1px 6px; border-radius: 8px; margin-bottom: 4px; }
    .product-card-body p { font-size: 11px; color: #666; line-height: 1.4; }
    .bg-blue { background: linear-gradient(135deg, #1a73e8, #4285f4); }
    .bg-green { background: linear-gradient(135deg, #34a853, #81c995); }
    .bg-orange { background: linear-gradient(135deg, #fbbc04, #fdd663); }
    .bg-red { background: linear-gradient(135deg, #ea4335, #f28b82); }
    .bg-purple { background: linear-gradient(135deg, #9c27b0, #ce93d8); }

    .dashboard-section { margin-bottom: 20px; }
    .dashboard-section h3 { color: #333; font-size: 14px; margin-bottom: 10px; padding-left: 6px;
                            border-left: 3px solid #1a73e8; }

    /* Login */
    .login-page { min-height: 100vh; display: flex; align-items: center; justify-content: center;
                  background: linear-gradient(135deg, #1a73e8 0%, #0d47a1 100%); }
    .login-box { background: white; border-radius: 14px; padding: 36px; width: 360px; box-shadow: 0 8px 32px rgba(0,0,0,.2); }
    .login-box h2 { text-align: center; margin-bottom: 6px; color: #1a73e8; }
    .login-box .subtitle { text-align: center; color: #999; font-size: 12px; margin-bottom: 20px; }
    .login-box .form-group { margin-bottom: 14px; }
    .login-box .form-group input { padding: 10px 12px; font-size: 13px; }
    .login-box .btn-primary { margin-top: 6px; }
    .login-box .links { display: flex; justify-content: space-between; margin-top: 14px; font-size: 12px; }
    .login-box .links a { color: #1a73e8; text-decoration: none; }
    .login-box .links a:hover { text-decoration: underline; }
    .login-box .flash { padding: 8px 12px; border-radius: 6px; font-size: 12px; margin-bottom: 14px; }
    .login-box .flash.error { background: #fff1f0; color: #cf1322; border: 1px solid #ffa39e; }
    .login-box .flash.success { background: #f6ffed; color: #389e0d; border: 1px solid #b7eb8f; }

    /* History */
    .history-list { padding: 16px; }
    .history-item { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px;
                    border: 1px solid #eee; border-radius: 8px; margin-bottom: 8px; transition: all .2s; background: white; }
    .history-item:hover { box-shadow: 0 2px 6px rgba(0,0,0,.08); border-color: #1a73e8; }
    .history-item-left { display: flex; align-items: center; gap: 12px; }
    .history-item-icon { width: 40px; height: 40px; border-radius: 8px; background: linear-gradient(135deg, #1a73e8, #4285f4);
                        display: flex; align-items: center; justify-content: center; color: white; font-size: 16px; }
    .history-item-info h4 { font-size: 13px; margin-bottom: 2px; color: #333; }
    .history-item-info .meta { font-size: 11px; color: #999; }
    .history-item-actions { display: flex; gap: 6px; }

    /* PDF export hidden area */
    #pdfExportArea { position: absolute; left: -9999px; top: 0; width: 800px; padding: 20px; background: white; }

    /* 问卷弹窗 */
    .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 10000;
                     display: none; justify-content: center; align-items: center; }
    .modal-overlay.active { display: flex; }
    .modal-box { background: white; border-radius: 10px; width: 90%; max-width: 600px; max-height: 85vh;
                 display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.15); }
    .modal-header { padding: 16px 20px; border-bottom: 1px solid #e8e8e8; display: flex;
                    justify-content: space-between; align-items: center; }
    .modal-header h3 { margin: 0; font-size: 15px; color: #1a73e8; }
    .modal-close { background: none; border: none; font-size: 18px; cursor: pointer; color: #999; }
    .modal-body { padding: 16px 20px; overflow-y: auto; flex: 1; }
    .q-item { margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid #f0f0f0; }
    .q-item:last-child { border-bottom: none; }
    .q-label { font-size: 13px; font-weight: 600; color: #333; margin-bottom: 6px; display: block; }
    .q-desc { font-size: 11px; color: #888; margin-bottom: 8px; }
    .q-input { width: 100%; padding: 8px 10px; border: 1px solid #d9d9d9; border-radius: 6px; font-size: 13px;
               box-sizing: border-box; font-family: inherit; }
    .q-input:focus { outline: none; border-color: #1a73e8; }
    .q-options { display: flex; flex-direction: column; gap: 6px; }
    .q-option { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
    .q-option input { margin: 0; }
    .q-file-area { margin-top: 8px; }
    .q-file-area input[type="file"] { font-size: 12px; color: #666; }
    .q-file-area .file-name { font-size: 11px; color: #1a73e8; margin-left: 6px; }
    .q-supplement { margin-top: 20px; padding-top: 16px; border-top: 2px solid #e8e8e8; }
    .q-supplement-label { font-size: 14px; font-weight: 600; color: #333; margin-bottom: 8px; display: block; }
    .q-supplement textarea { width: 100%; padding: 10px; border: 1px solid #d9d9d9; border-radius: 6px;
        font-size: 13px; font-family: inherit; resize: vertical; min-height: 80px; }
    .q-supplement textarea:focus { outline: none; border-color: #1a73e8; }
    .q-supplement-hint { font-size: 11px; color: #999; margin-top: 4px; }
    .modal-footer { padding: 12px 20px; border-top: 1px solid #e8e8e8; display: flex;
                    justify-content: flex-end; gap: 8px; }
    .btn-primary { background: #1a73e8; color: #fff; border: none; padding: 8px 16px; border-radius: 6px;
                   font-size: 13px; cursor: pointer; }
    .btn-primary:hover { background: #1557b0; }
    .btn-primary:disabled { background: #a0c4ff; cursor: not-allowed; }
</style>
"""


# ==================== 页面模板函数 ====================

def render_page(title, body, username=None):
    """渲染带header的页面"""
    header = ""
    if username:
        header = f"""
        <div class="header">
            <div class="header-left">
                <h1><i class="fas fa-robot"></i> B端智能售前方案生成系统</h1>
                <span class="badge">v3.2</span>
            </div>
            <div class="header-right">
                <a href="/"><i class="fas fa-home"></i> 首页</a>
                <a href="/history"><i class="fas fa-history"></i> 历史方案</a>
                <span><i class="fas fa-user-circle"></i> {username}</span>
                <a href="/logout"><i class="fas fa-sign-out-alt"></i> 退出</a>
            </div>
        </div>
        """
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} | B端智能售前方案生成系统</title>
    {COMMON_HEAD}
</head>
<body>
    {header}
    {body}
    <!-- PDF导出专用隐藏区域 -->
    <div id="pdfExportArea"></div>
</body>
</html>"""


def get_flashes_html():
    """获取flash消息HTML"""
    msgs = []
    # 由于不在Jinja2模板中，我们手动处理flash
    # 实际上我们会在路由中直接构造HTML
    return ""


# ==================== 路由 ====================

@app.route("/login")
def login_page():
    if "username" in session:
        return redirect(url_for("index"))
    # 手动构造flash消息HTML
    flash_html = ""
    from flask import get_flashed_messages
    for category, message in get_flashed_messages(with_categories=True):
        flash_html += f'<div class="flash {category}">{message}</div>'

    body = f"""
    <div class="login-page">
        <div class="login-box">
            <h2><i class="fas fa-robot"></i> 智能售前系统</h2>
            <div class="subtitle">B端智能售前方案生成系统 v3.2</div>
            {flash_html}
            <form method="POST" action="/login">
                <div class="form-group">
                    <label>用户名</label>
                    <input type="text" name="username" placeholder="输入用户名" required>
                </div>
                <div class="form-group">
                    <label>密码</label>
                    <input type="password" name="password" placeholder="输入密码" required>
                </div>
                <button type="submit" class="btn-primary"><i class="fas fa-sign-in-alt"></i> 登录</button>
            </form>
            <div class="links">
                <span>默认: admin / admin123</span>
                <a href="/register">注册新账号</a>
            </div>
        </div>
    </div>
    """
    return render_page("登录", body)


@app.route("/login", methods=["POST"])
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    users = _load_users()
    if username in users and users[username]["password"] == _hash_password(password):
        session["username"] = username
        return redirect(url_for("index"))
    flash("用户名或密码错误", "error")
    return redirect(url_for("login_page"))


@app.route("/register")
def register_page():
    if "username" in session:
        return redirect(url_for("index"))
    flash_html = ""
    from flask import get_flashed_messages
    for category, message in get_flashed_messages(with_categories=True):
        flash_html += f'<div class="flash {category}">{message}</div>'

    body = f"""
    <div class="login-page">
        <div class="login-box">
            <h2><i class="fas fa-user-plus"></i> 注册账号</h2>
            <div class="subtitle">创建新用户以保存和管理方案</div>
            {flash_html}
            <form method="POST" action="/register">
                <div class="form-group">
                    <label>用户名</label>
                    <input type="text" name="username" placeholder="3-20位字母数字" required minlength="3" maxlength="20">
                </div>
                <div class="form-group">
                    <label>密码</label>
                    <input type="password" name="password" placeholder="至少6位" required minlength="6">
                </div>
                <div class="form-group">
                    <label>确认密码</label>
                    <input type="password" name="confirm" placeholder="再次输入密码" required minlength="6">
                </div>
                <button type="submit" class="btn-primary"><i class="fas fa-user-plus"></i> 注册</button>
            </form>
            <div class="links">
                <span></span>
                <a href="/login">已有账号？去登录</a>
            </div>
        </div>
    </div>
    """
    return render_page("注册", body)


@app.route("/register", methods=["POST"])
def register_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if not username or not password:
        flash("用户名和密码不能为空", "error")
        return redirect(url_for("register_page"))
    if password != confirm:
        flash("两次输入的密码不一致", "error")
        return redirect(url_for("register_page"))
    users = _load_users()
    if username in users:
        flash("用户名已存在", "error")
        return redirect(url_for("register_page"))
    users[username] = {"password": _hash_password(password), "created_at": datetime.now().isoformat()}
    _save_users(users)
    flash("注册成功，请登录", "success")
    return redirect(url_for("login_page"))


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    body = r"""
    <div class="container">
        <div class="panel panel-left">
            <h3><i class="fas fa-edit"></i> 输入区</h3>
            <div class="form-group">
                <label><i class="fas fa-building"></i> 客户名称</label>
                <input id="customerName" value="某师范大学" placeholder="如：某师范大学">
            </div>
            <div class="form-group">
                <label><i class="fas fa-industry"></i> 行业领域</label>
                <select id="industry">
                    <option value="education">education（教育）</option>
                    <option value="healthcare">healthcare（医疗）</option>
                    <option value="manufacturing">manufacturing（制造）</option>
                    <option value="finance">finance（金融）</option>
                </select>
            </div>
            <div class="form-group">
                <label><i class="fas fa-comment-dots"></i> 客户需求描述</label>
                <textarea id="customerInput" placeholder="越详细越精准...">我们是师范大学，想建设一批智慧教室，主要用来训练师范生的课堂管理和板书能力，预算在200万左右。</textarea>
            </div>

            <!-- 语音录入区域 -->
            <div class="voice-section" id="voiceSection">
                <div class="voice-header">
                    <i class="fas fa-microphone"></i> <span>语音录入</span>
                    <span class="voice-hint">录音：适合1分钟内短音频 | 上传：支持MP3/WAV等，最长5小时</span>
                </div>
                <div class="voice-controls">
                    <button type="button" class="btn-record" id="btnRecord" onclick="toggleRecording()">
                        <i class="fas fa-circle" id="recordIcon"></i> <span id="recordText">开始录音</span>
                    </button>
                    <label class="btn-upload">
                        <i class="fas fa-upload"></i> 上传音频
                        <input type="file" id="audioUpload" accept="audio/*" style="display:none" onchange="uploadAudio(this)">
                    </label>
                </div>
                <div class="voice-status" id="voiceStatus"></div>
                <div class="voice-result" id="voiceResult" style="display:none;">
                    <div class="voice-result-label"><i class="fas fa-file-audio"></i> 转写结果</div>
                    <div class="voice-transcript" id="voiceTranscript"></div>
                    <div class="voice-actions">
                        <button type="button" class="btn-secondary" onclick="fillFormFromVoice()">
                            <i class="fas fa-magic"></i> 一键填充表单
                        </button>
                        <button type="button" class="btn-secondary" onclick="appendToInput()">
                            <i class="fas fa-plus"></i> 追加到需求
                        </button>
                    </div>
                </div>
            </div>

            <button class="btn-primary" id="generateBtn" onclick="runPipeline()">
                <i class="fas fa-magic"></i> <span>生成售前方案</span>
            </button>
            <div class="status-box" id="statusBox">等待开始...</div>
            <div class="tips">
                <i class="fas fa-info-circle"></i> <strong>提示</strong><br>
                &bull; 需求描述越详细，方案越精准<br>
                &bull; 生成约需30-60秒（4次LLM调用）<br>
                &bull; 方案自动保存到历史记录
            </div>
        </div>

        <div class="panel panel-right">
            <div class="tabs">
                <button class="tab-btn active" onclick="switchTab('proposal')" data-tab="proposal"><i class="fas fa-file-alt"></i> 售前方案</button>
                <button class="tab-btn" onclick="switchTab('dashboard')" data-tab="dashboard"><i class="fas fa-chart-pie"></i> 数据看板</button>
                <button class="tab-btn" onclick="switchTab('products')" data-tab="products"><i class="fas fa-box"></i> 产品方案</button>
                <button class="tab-btn" onclick="switchTab('intent')" data-tab="intent"><i class="fas fa-brain"></i> 需求解析</button>
                <button class="tab-btn" onclick="switchTab('competitor')" data-tab="competitor"><i class="fas fa-shield-alt"></i> 竞品分析</button>
            </div>

            <div class="tab-content active" id="tab-proposal">
                <div class="btn-group">
                    <button class="btn-secondary" onclick="exportPDF()"><i class="fas fa-file-pdf"></i> 导出PDF</button>
                    <button class="btn-secondary" onclick="exportMarkdown()"><i class="fas fa-download"></i> 下载Markdown</button>
                    <button class="btn-secondary" id="btnQuestionnaire" onclick="generateQuestionnaire()" style="display:none;"><i class="fas fa-clipboard-list"></i> 补充问卷</button>
                </div>
                <div id="proposalOutput" style="margin-top: 10px;">
                    <p class="placeholder-text"><i class="fas fa-arrow-left"></i> 输入需求后点击"生成售前方案"</p>
                </div>
            </div>

            <div class="tab-content" id="tab-dashboard">
                <div class="dashboard-section">
                    <h3><i class="fas fa-chart-pie"></i> 需求覆盖分析</h3>
                    <div class="chart-row">
                        <div class="chart-box" id="chartCoverage"></div>
                        <div class="chart-box" id="chartBudget"></div>
                    </div>
                </div>
                <div class="dashboard-section">
                    <h3><i class="fas fa-chart-bar"></i> 产品匹配评分</h3>
                    <div class="chart-full" id="chartProducts"></div>
                </div>
                <div class="dashboard-section">
                    <h3><i class="fas fa-chart-radar"></i> 竞品对比雷达图</h3>
                    <div class="chart-full" id="chartCompetitor"></div>
                </div>
                <div class="dashboard-section">
                    <h3><i class="fas fa-road"></i> 实施路线图</h3>
                    <div class="chart-full" id="chartTimeline"></div>
                </div>
            </div>

            <div class="tab-content" id="tab-products">
                <div class="product-cards" id="productCards">
                    <p class="placeholder-text">生成方案后，匹配的产品信息将展示在这里</p>
                </div>
            </div>

            <div class="tab-content" id="tab-intent">
                <div id="intentOutput" style="font-size:13px;line-height:1.7;">
                    <p class="placeholder-text">生成方案后，需求解析结果将展示在这里</p>
                </div>
            </div>

            <div class="tab-content" id="tab-competitor">
                <div id="competitorOutput">
                    <p class="placeholder-text">竞品对比分析与投标策略将显示在这里</p>
                </div>
            </div>
        </div>
    </div>

<script>
let resultData = null;
let chartInstances = {};

function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector(`.tab-btn[data-tab="${name}"]`)?.classList.add('active');
    document.getElementById(`tab-${name}`)?.classList.add('active');
    if (name === 'dashboard' && resultData) {
        // 使用双requestAnimationFrame确保浏览器布局完成后再渲染图表
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                renderCharts();
            });
        });
    }
    saveState();
}

// ==================== Markdown → HTML (客户端) ====================
function mdToHtml(md) {
    if (!md) return '';
    var lines = md.split('\n');
    var out = [];
    var inCode = false, codeBuf = [];
    var inTable = false, tableRows = [];
    var inList = false, listItems = [], listType = null;

    function flushTable() {
        if (!tableRows.length) { inTable = false; return; }
        var dataRows = [];
        for (var i = 0; i < tableRows.length; i++) {
            var row = tableRows[i];
            var cells = row.split('|').map(function(c){ return c.trim(); }).filter(function(c){ return c !== ''; });
            if (cells.length && cells.every(function(c){ return /^[\-:\s|]+$/.test(c); })) continue;
            if (cells.length) dataRows.push(cells);
        }
        if (dataRows.length) {
            var thead = '<tr>' + dataRows[0].map(function(c){ return '<th>' + inline(c) + '</th>'; }).join('') + '</tr>';
            var tbody = '';
            for (var j = 1; j < dataRows.length; j++) {
                tbody += '<tr>' + dataRows[j].map(function(c){ return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
            }
            out.push('<table><thead>' + thead + '</thead><tbody>' + tbody + '</tbody></table>');
        }
        inTable = false; tableRows = [];
    }
    function flushList() {
        if (listItems.length) {
            var tag = listType === 'ol' ? 'ol' : 'ul';
            out.push('<' + tag + '>' + listItems.join('') + '</' + tag + '>');
        }
        inList = false; listItems = []; listType = null;
    }
    function flushCode() {
        if (codeBuf.length) out.push('<pre><code>' + esc(codeBuf.join('\n')) + '</code></pre>');
        inCode = false; codeBuf = [];
    }

    for (var idx = 0; idx < lines.length; idx++) {
        var line = lines[idx];
        var s = line.trim();

        if (s.indexOf('```') === 0) {
            if (inCode) { flushCode(); }
            else { flushTable(); flushList(); inCode = true; }
            continue;
        }
        if (inCode) { codeBuf.push(line); continue; }
        if (s === '') { flushTable(); flushList(); continue; }

        if (s.charAt(0) === '|' && s.indexOf('|', 1) > -1) {
            flushList();
            if (!inTable) inTable = true;
            tableRows.push(s);
            continue;
        } else { flushTable(); }

        if (s.indexOf('###### ') === 0) { flushList(); out.push('<h6>' + inline(s.substring(7)) + '</h6>'); continue; }
        if (s.indexOf('##### ') === 0) { flushList(); out.push('<h5>' + inline(s.substring(6)) + '</h5>'); continue; }
        if (s.indexOf('#### ') === 0) { flushList(); out.push('<h4>' + inline(s.substring(5)) + '</h4>'); continue; }
        if (s.indexOf('### ') === 0) { flushList(); out.push('<h3>' + inline(s.substring(4)) + '</h3>'); continue; }
        if (s.indexOf('## ') === 0) { flushList(); out.push('<h2>' + inline(s.substring(3)) + '</h2>'); continue; }
        if (s.indexOf('# ') === 0) { flushList(); out.push('<h1>' + inline(s.substring(2)) + '</h1>'); continue; }

        if (s.indexOf('- ') === 0 || s.indexOf('* ') === 0) {
            if (!inList || listType !== 'ul') { flushList(); inList = true; listType = 'ul'; }
            listItems.push('<li>' + inline(s.substring(2)) + '</li>');
            continue;
        }
        if (/^\d+\.\s/.test(s)) {
            if (!inList || listType !== 'ol') { flushList(); inList = true; listType = 'ol'; }
            listItems.push('<li>' + inline(s.replace(/^\d+\.\s/, '')) + '</li>');
            continue;
        }

        flushList();
        out.push('<p>' + inline(s) + '</p>');
    }
    flushTable(); flushList(); flushCode();
    return '<div class="markdown-body">' + out.join('\n') + '</div>';
}

function esc(t) {
    return t.replace(/<br\s*\/?>/gi, ' ')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ==================== 语音录入 ====================
var audioContext = null;
var audioStream = null;
var audioProcessor = null;
var audioSource = null;
var recordedSamples = [];
var isRecording = false;
var recordingStartTime = 0;
var recordingTimer = null;

function encodeWAV(samples, sampleRate) {
    var buffer = new ArrayBuffer(44 + samples.length * 2);
    var view = new DataView(buffer);
    function writeString(view, offset, string) {
        for (var i = 0; i < string.length; i++) {
            view.setUint8(offset + i, string.charCodeAt(i));
        }
    }
    writeString(view, 0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeString(view, 8, 'WAVE');
    writeString(view, 12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(view, 36, 'data');
    view.setUint32(40, samples.length * 2, true);
    for (var i = 0; i < samples.length; i++) {
        var s = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return new Blob([view], { type: 'audio/wav' });
}

function toggleRecording() {
    var btn = document.getElementById('btnRecord');
    var icon = document.getElementById('recordIcon');
    var text = document.getElementById('recordText');
    var status = document.getElementById('voiceStatus');

    if (!isRecording) {
        // 开始录音
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            alert('当前浏览器不支持录音功能，请使用 Chrome/Edge/Firefox 最新版');
            return;
        }
        navigator.mediaDevices.getUserMedia({ audio: true }).then(function(stream) {
            audioStream = stream;
            audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            audioSource = audioContext.createMediaStreamSource(stream);
            audioProcessor = audioContext.createScriptProcessor(4096, 1, 1);
            recordedSamples = [];
            recordingStartTime = Date.now();

            audioProcessor.onaudioprocess = function(e) {
                var inputData = e.inputBuffer.getChannelData(0);
                recordedSamples.push(new Float32Array(inputData));
            };

            audioSource.connect(audioProcessor);
            audioProcessor.connect(audioContext.destination);

            isRecording = true;
            btn.classList.add('recording');
            icon.className = 'fas fa-stop';
            text.textContent = '停止录音';
            status.textContent = '正在录音...';

            // 录音时长显示
            recordingTimer = setInterval(function() {
                var sec = Math.floor((Date.now() - recordingStartTime) / 1000);
                status.textContent = '正在录音... ' + sec + '秒';
            }, 1000);
        }).catch(function(err) {
            alert('无法访问麦克风: ' + err.message);
        });
    } else {
        // 停止录音
        if (recordingTimer) clearInterval(recordingTimer);
        if (audioProcessor) {
            audioProcessor.disconnect();
            audioProcessor.onaudioprocess = null;
        }
        if (audioSource) audioSource.disconnect();
        if (audioContext) audioContext.close();
        if (audioStream) audioStream.getTracks().forEach(function(t) { t.stop(); });

        // 合并采样数据
        var totalLength = 0;
        for (var i = 0; i < recordedSamples.length; i++) totalLength += recordedSamples[i].length;
        var merged = new Float32Array(totalLength);
        var offset = 0;
        for (var i = 0; i < recordedSamples.length; i++) {
            merged.set(recordedSamples[i], offset);
            offset += recordedSamples[i].length;
        }

        var sampleRate = audioContext ? audioContext.sampleRate : 16000;
        var blob = encodeWAV(merged, sampleRate);
        sendAudioToServer(blob, 'recording.wav');

        isRecording = false;
        btn.classList.remove('recording');
        icon.className = 'fas fa-circle';
        text.textContent = '开始录音';
        status.textContent = '正在转写，请稍候...';
    }
}

function uploadAudio(input) {
    if (!input.files.length) return;
    var file = input.files[0];
    var status = document.getElementById('voiceStatus');
    status.textContent = '正在上传文件: ' + file.name + '...';
    sendAudioFileToServer(file, file.name);
    input.value = '';
}

function sendAudioToServer(blob, filename) {
    // 实时录音（短音频）→ 语音听写API
    var status = document.getElementById('voiceStatus');
    var formData = new FormData();
    formData.append('audio', blob, filename);
    fetch('/api/transcribe', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                status.textContent = '转写失败: ' + data.error;
                return;
            }
            document.getElementById('voiceTranscript').textContent = data.text;
            document.getElementById('voiceResult').style.display = 'block';
            status.textContent = '转写完成，共 ' + data.text.length + ' 字';
            saveState();
        })
        .catch(function(err) {
            status.textContent = '请求失败: ' + err.message;
        });
}

function sendAudioFileToServer(file, filename) {
    // 文件上传（支持长音频、MP3等格式）→ 录音文件转写API
    var status = document.getElementById('voiceStatus');
    var formData = new FormData();
    formData.append('audio', file, filename);
    fetch('/api/transcribe_file', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                status.textContent = '上传失败: ' + data.error;
                return;
            }
            status.textContent = '文件已上传，正在转写中（这可能需要几分钟）...';
            pollTranscribeProgress(data.task_id);
        })
        .catch(function(err) {
            status.textContent = '请求失败: ' + err.message;
        });
}

function pollTranscribeProgress(taskId) {
    var status = document.getElementById('voiceStatus');
    var pollInterval = setInterval(function() {
        fetch('/api/transcribe_file/progress/' + taskId)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.status === 'done') {
                    clearInterval(pollInterval);
                    document.getElementById('voiceTranscript').textContent = data.text;
                    document.getElementById('voiceResult').style.display = 'block';
                    status.textContent = '转写完成，共 ' + data.text.length + ' 字';
                    saveState();
                } else if (data.status === 'error') {
                    clearInterval(pollInterval);
                    status.textContent = '转写失败: ' + (data.error || '未知错误');
                } else {
                    status.textContent = '转写进度: ' + (data.status || '处理中...');
                }
            })
            .catch(function(err) {
                // 轮询出错不停止，继续尝试
                status.textContent = '正在查询进度...';
            });
    }, 5000); // 每5秒轮询一次
}

function fillFormFromVoice() {
    var transcript = document.getElementById('voiceTranscript').textContent;
    if (!transcript.trim()) return;
    var status = document.getElementById('voiceStatus');
    status.textContent = '正在提炼需求...';
    fetch('/api/extract_needs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: transcript })
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.error) { status.textContent = '提炼失败: ' + data.error; return; }
        var ext = data.extracted || {};
        if (ext.customer_name) document.getElementById('customerName').value = ext.customer_name;
        if (ext.industry) document.getElementById('industry').value = ext.industry;
        if (ext.customer_input) document.getElementById('customerInput').value = ext.customer_input;
        status.textContent = '已自动填充表单，请检查并补充后生成方案';
    }).catch(function(err) { status.textContent = '请求失败: ' + err.message; });
}

function appendToInput() {
    var transcript = document.getElementById('voiceTranscript').textContent;
    if (!transcript.trim()) return;
    var input = document.getElementById('customerInput');
    if (input.value.trim()) {
        input.value += '\n\n' + transcript;
    } else {
        input.value = transcript;
    }
    document.getElementById('voiceStatus').textContent = '已追加到需求描述';
}

function inline(t) {
    // 先处理code避免干扰
    var parts = [], i = 0;
    while (i < t.length) {
        if (t.charAt(i) === '`') {
            var end = t.indexOf('`', i+1);
            if (end !== -1) { parts.push('<code>' + esc(t.substring(i+1, end)) + '</code>'); i = end + 1; continue; }
        }
        if (t.substring(i, i+2) === '**') {
            var end = t.indexOf('**', i+2);
            if (end !== -1) { parts.push('<strong>' + inline(t.substring(i+2, end)) + '</strong>'); i = end + 2; continue; }
        }
        // 斜体 * (排除 **)
        if (t.charAt(i) === '*' && t.charAt(i+1) !== '*') {
            var end = t.indexOf('*', i+1);
            if (end !== -1 && t.charAt(end+1) !== '*') {
                parts.push('<em>' + inline(t.substring(i+1, end)) + '</em>'); i = end + 1; continue;
            }
        }
        parts.push(esc(t.charAt(i)));
        i++;
    }
    return parts.join('');
}

// ==================== 图表 ====================
function renderCharts() {
    if (!resultData) return;
    var intent = resultData.intent_result || {};
    var matching = resultData.matching_result || {};
    var reqs = intent.requirements || {};
    var funcCnt = (reqs.functional || []).length;
    var nonFuncCnt = (reqs.non_functional || []).length;
    var implicitCnt = (intent.implicit_needs || []).length;

    // 每次重新初始化echarts实例，避免tab切换时尺寸为0的问题
    Object.keys(chartInstances).forEach(function(key) {
        if (chartInstances[key]) { chartInstances[key].dispose(); delete chartInstances[key]; }
    });

    // 确保图表容器有明确尺寸（防止flex布局下尺寸计算延迟）
    ['chartCoverage','chartBudget','chartProducts','chartCompetitor','chartTimeline'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el && el.clientHeight < 10) { el.style.height = '280px'; }
    });

    if (document.getElementById('chartCoverage')) chartInstances.coverage = echarts.init(document.getElementById('chartCoverage'));
    if (chartInstances.coverage) chartInstances.coverage.setOption({
        title: { text: '需求类型分布', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        series: [{
            type: 'pie', radius: ['38%', '62%'], center: ['50%', '55%'],
            data: [
                { value: funcCnt || 1, name: '功能需求', itemStyle: { color: '#1a73e8' } },
                { value: nonFuncCnt || 1, name: '非功能需求', itemStyle: { color: '#34a853' } },
                { value: implicitCnt || 1, name: '隐性需求', itemStyle: { color: '#fbbc04' } }
            ],
            label: { formatter: '{b}\n{d}%', fontSize: 11 }
        }]
    });

    if (document.getElementById('chartBudget')) chartInstances.budget = echarts.init(document.getElementById('chartBudget'));
    if (chartInstances.budget) chartInstances.budget.setOption({
        title: { text: '预算分配估算', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: { trigger: 'item', formatter: '{b}: {c}万元 ({d}%)' },
        series: [{
            type: 'pie', radius: ['38%', '62%'],
            data: [
                { value: 120, name: '硬件设备', itemStyle: { color: '#1a73e8' } },
                { value: 30, name: '软件平台', itemStyle: { color: '#34a853' } },
                { value: 30, name: 'AI服务', itemStyle: { color: '#ea4335' } },
                { value: 20, name: '实施服务', itemStyle: { color: '#fbbc04' } }
            ],
            label: { formatter: '{b}\n{c}万', fontSize: 11 }
        }]
    });

    var products = matching.matching_result || [];
    if (products.length > 0) {
        // 按类别统计产品数量
        var categoryMap = {};
        products.forEach(function(p) {
            var cat = p.category || '其他';
            if (!categoryMap[cat]) categoryMap[cat] = 0;
            categoryMap[cat]++;
        });
        var pieData = Object.keys(categoryMap).map(function(cat) {
            return { name: cat, value: categoryMap[cat] };
        });
        if (document.getElementById('chartProducts')) chartInstances.products = echarts.init(document.getElementById('chartProducts'));
        if (chartInstances.products) chartInstances.products.setOption({
            title: { text: '推荐产品类别分布', left: 'center', textStyle: { fontSize: 13 } },
            tooltip: { trigger: 'item', formatter: '{b}: {c}个 ({d}%)' },
            legend: { bottom: 0, textStyle: { fontSize: 10 } },
            series: [{
                type: 'pie', radius: ['38%', '62%'], center: ['50%', '50%'],
                data: pieData,
                label: { formatter: '{b}\n{d}%', fontSize: 10 }
            }]
        });
    }

    if (document.getElementById('chartCompetitor')) chartInstances.competitor = echarts.init(document.getElementById('chartCompetitor'));
    if (chartInstances.competitor) chartInstances.competitor.setOption({
        title: { text: '竞品能力对比', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: {},
        legend: { data: ['我方方案', '希沃', '腾讯教育', '阿里钉钉'], bottom: 0, textStyle: { fontSize: 11 } },
        radar: {
            indicator: [
                { name: 'AI评估能力', max: 100 }, { name: '硬件产品力', max: 100 },
                { name: '方案完整性', max: 100 }, { name: '教育场景深度', max: 100 },
                { name: '服务交付能力', max: 100 }, { name: '性价比', max: 100 }
            ],
            center: ['50%', '50%'], radius: '55%', axisName: { fontSize: 10 }
        },
        series: [{
            type: 'radar', symbol: 'none',
            data: [
                { value: [95,85,90,95,90,80], name: '我方方案', lineStyle: { color: '#1a73e8', width: 2 }, areaStyle: { color: 'rgba(26,115,232,.12)' } },
                { value: [40,90,60,70,75,70], name: '希沃', lineStyle: { color: '#ea4335', width: 1.5 } },
                { value: [60,50,75,55,80,85], name: '腾讯教育', lineStyle: { color: '#34a853', width: 1.5 } },
                { value: [50,75,65,80,60,75], name: '阿里钉钉', lineStyle: { color: '#fbbc04', width: 1.5 } }
            ]
        }]
    });

    if (document.getElementById('chartTimeline')) chartInstances.timeline = echarts.init(document.getElementById('chartTimeline'));
    if (chartInstances.timeline) chartInstances.timeline.setOption({
        title: { text: '项目阶段规划', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: { trigger: 'axis' },
        grid: { left: '3%', right: '4%', bottom: '5%', top: '15%', containLabel: true },
        xAxis: { type: 'category', data: ['需求调研','方案设计','硬件部署','软件开发','试运行','验收交付'],
                 axisLabel: { fontSize: 10 } },
        yAxis: { type: 'value', name: '周期(周)' },
        series: [{
            type: 'bar', data: [2,2,4,6,4,2], barWidth: '30%',
            label: { show: true, position: 'top', formatter: '{c}周', fontSize: 10 },
            itemStyle: { color: new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:'#1a73e8'},{offset:1,color:'#0d47a1'}]), borderRadius: [3,3,0,0] }
        }]
    });
}

// ==================== 需求解析展示 ====================
function renderIntentResult(intent) {
    if (!intent) return '<p class="placeholder-text">暂无需求解析数据</p>';
    var profile = intent.customer_profile || {};
    var reqs = intent.requirements || {};
    var funcReqs = reqs.functional || [];
    var nonFuncReqs = reqs.non_functional || [];
    var implicit = intent.implicit_needs || [];
    var painPoints = profile.pain_points || [];
    var decisionMakers = profile.decision_makers || [];

    function badgeClass(p) {
        if (p === 'P0') return 'background:#cf1322;color:#fff;';
        if (p === 'P1') return 'background:#fa8c16;color:#fff;';
        return 'background:#8c8c8c;color:#fff;';
    }
    function clarityClass(c) {
        if (c === '清晰') return 'color:#389e0d;';
        if (c === '模糊') return 'color:#fa8c16;';
        return 'color:#666;';
    }

    var html = '<div style="padding:10px 0;">';

    // 客户概况
    html += '<div style="margin-bottom:18px;padding:14px;background:#f6f8fa;border-radius:8px;">';
    html += '<h3 style="font-size:14px;color:#1a73e8;margin-bottom:10px;"><i class="fas fa-building"></i> 客户概况</h3>';
    if (profile.industry) html += '<p><strong>行业：</strong>' + esc(profile.industry) + '</p>';
    if (profile.scale) html += '<p><strong>规模：</strong>' + esc(profile.scale) + '</p>';
    if (decisionMakers.length) {
        html += '<p><strong>决策链：</strong></p><ul style="margin:4px 0 8px 18px;">';
        decisionMakers.forEach(function(d){ html += '<li>' + esc(d) + '</li>'; });
        html += '</ul>';
    }
    if (painPoints.length) {
        html += '<p><strong>核心痛点：</strong></p><ol style="margin:4px 0 0 18px;">';
        painPoints.forEach(function(p, i){ html += '<li>' + esc(p) + '</li>'; });
        html += '</ol>';
    }
    html += '</div>';

    // 功能需求
    if (funcReqs.length) {
        html += '<div style="margin-bottom:18px;">';
        html += '<h3 style="font-size:14px;color:#1a73e8;margin-bottom:8px;"><i class="fas fa-list-check"></i> 功能需求 (' + funcReqs.length + '条)</h3>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
        html += '<thead><tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">需求描述</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:center;width:50px;">优先级</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:center;width:60px;">清晰度</th></tr></thead><tbody>';
        funcReqs.forEach(function(r) {
            html += '<tr>';
            html += '<td style="padding:6px 8px;border:1px solid #ddd;">' + esc(r.requirement || '') + '</td>';
            html += '<td style="padding:6px 8px;border:1px solid #ddd;text-align:center;"><span style="display:inline-block;padding:1px 6px;border-radius:10px;font-size:11px;' + badgeClass(r.priority) + '">' + esc(r.priority || '-') + '</span></td>';
            html += '<td style="padding:6px 8px;border:1px solid #ddd;text-align:center;"><span style="font-size:11px;' + clarityClass(r.clarity) + '">' + esc(r.clarity || '-') + '</span></td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    }

    // 非功能需求
    if (nonFuncReqs.length) {
        html += '<div style="margin-bottom:18px;">';
        html += '<h3 style="font-size:14px;color:#1a73e8;margin-bottom:8px;"><i class="fas fa-shield-alt"></i> 非功能需求 (' + nonFuncReqs.length + '条)</h3>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
        html += '<thead><tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">需求描述</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:center;width:50px;">优先级</th></tr></thead><tbody>';
        nonFuncReqs.forEach(function(r) {
            html += '<tr>';
            html += '<td style="padding:6px 8px;border:1px solid #ddd;">' + esc(r.requirement || '') + '</td>';
            html += '<td style="padding:6px 8px;border:1px solid #ddd;text-align:center;"><span style="display:inline-block;padding:1px 6px;border-radius:10px;font-size:11px;' + badgeClass(r.priority) + '">' + esc(r.priority || '-') + '</span></td>';
            html += '</tr>';
        });
        html += '</tbody></table></div>';
    }

    // 隐性需求
    if (implicit.length) {
        html += '<div style="margin-bottom:18px;">';
        html += '<h3 style="font-size:14px;color:#1a73e8;margin-bottom:8px;"><i class="fas fa-lightbulb"></i> 隐性需求 (' + implicit.length + '条)</h3>';
        html += '<ul style="margin:0 0 0 18px;padding:0;">';
        implicit.forEach(function(item) {
            var text = (typeof item === 'string') ? item : (item.need || item.description || JSON.stringify(item));
            html += '<li style="margin:4px 0;">' + esc(text) + '</li>';
        });
        html += '</ul></div>';
    }

    html += '</div>';
    return html;
}

// ==================== 产品卡片 ====================
var productIconMap = {
    '黑板': { icon: 'fa-chalkboard', bg: 'bg-blue' },
    '课堂': { icon: 'fa-laptop', bg: 'bg-green' },
    '教师': { icon: 'fa-user-tie', bg: 'bg-orange' },
    '数据': { icon: 'fa-database', bg: 'bg-purple' },
    '评估': { icon: 'fa-chart-line', bg: 'bg-red' },
    '考试': { icon: 'fa-clipboard-check', bg: 'bg-red' },
    'default': { icon: 'fa-cube', bg: 'bg-blue' }
};

function renderProductCards() {
    if (!resultData) return;
    var products = (resultData.matching_result || {}).matching_result || [];
    if (!products.length) return;
    // 按匹配度从高到低排序
    products = products.slice().sort(function(a, b) {
        return (b.match_score || 0) - (a.match_score || 0);
    });
    var html = '';
    products.forEach(function(p) {
        var name = p.product_name || '未知产品';
        var key = Object.keys(productIconMap).find(function(k){ return name.indexOf(k) !== -1; }) || 'default';
        var cfg = productIconMap[key];
        html += '<div class="product-card">' +
            '<div class="product-card-icon ' + cfg.bg + '"><i class="fas ' + cfg.icon + '"></i></div>' +
            '<div class="product-card-body">' +
            '<span class="tag">' + (p.match_score || '') + '% 匹配</span>' +
            '<h4>' + esc(name) + '</h4>' +
            '<p>' + esc(p.description || p.differentiation || '') + '</p>' +
            '</div></div>';
    });
    document.getElementById('productCards').innerHTML = html;
}

// ==================== 生成方案 ====================
async function runPipeline() {
    var customerName = document.getElementById('customerName').value || '客户';
    var customerInput = document.getElementById('customerInput').value;
    var industry = document.getElementById('industry').value;
    var btn = document.getElementById('generateBtn');
    var statusBox = document.getElementById('statusBox');

    if (!customerInput.trim()) { statusBox.innerHTML = '错误：客户需求不能为空'; statusBox.className = 'status-box error'; return; }

    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 生成中...';
    statusBox.innerHTML = '<div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div><div class="progress-text">准备开始...</div>';
    statusBox.className = 'status-box';

    try {
        var resp = await fetch('/api/generate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ customer_name: customerName, customer_input: customerInput, industry: industry })
        });
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var completed = false;

        while (true) {
            var { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            var lines = buffer.split('\n\n');
            buffer = lines.pop();
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i].trim();
                if (line.indexOf('data: ') === 0) {
                    var evtData = line.substring(6);
                    try {
                        var evt = JSON.parse(evtData);
                        if (evt.error) {
                            statusBox.innerHTML = '错误: ' + evt.error;
                            statusBox.className = 'status-box error';
                            btn.disabled = false; btn.innerHTML = '<i class="fas fa-magic"></i> <span>生成售前方案</span>';
                            return;
                        }
                        if (evt.progress !== undefined) {
                            var fill = statusBox.querySelector('.progress-fill');
                            var text = statusBox.querySelector('.progress-text');
                            if (fill) fill.style.width = evt.progress + '%';
                            if (text) text.textContent = evt.status || ('进度 ' + evt.progress + '%');
                        }
                        if (evt.result) {
                            completed = true;
                            resultData = evt.result;
                            document.getElementById('proposalOutput').innerHTML = mdToHtml(evt.result.proposal);
                            document.getElementById('intentOutput').innerHTML = renderIntentResult(evt.result.intent_result);
                            document.getElementById('competitorOutput').innerHTML = mdToHtml(evt.result.competitor_analysis);
                            renderProductCards();
                            renderCharts();
                            statusBox.innerHTML = '<div class="progress-bar"><div class="progress-fill" style="width:100%"></div></div><div class="progress-text" style="color:#34a853;font-weight:600;"><i class="fas fa-check-circle"></i> 方案生成完成！已保存到历史记录</div>';
                            statusBox.className = 'status-box success';
                            document.getElementById('btnQuestionnaire').style.display = 'inline-block';
                            switchTab('proposal');
                            saveState();
                        }
                    } catch(e) {}
                }
            }
        }
        if (!completed) {
            statusBox.innerHTML = '请求异常结束，未收到完整结果';
            statusBox.className = 'status-box error';
        }
    } catch (err) {
        statusBox.innerHTML = '请求失败: ' + err.message;
        statusBox.className = 'status-box error';
    } finally {
        btn.disabled = false; btn.innerHTML = '<i class="fas fa-magic"></i> <span>生成售前方案</span>';
    }
}

// ==================== PDF导出 ====================
function exportPDF() {
    var proposalEl = document.getElementById('proposalOutput');
    var customerName = document.getElementById('customerName').value || '客户';
    if (!proposalEl.innerHTML || proposalEl.innerHTML.trim() === '' || proposalEl.innerHTML.indexOf('placeholder-text') > -1) {
        alert('请先生成方案再导出PDF'); return;
    }

    // 使用 iframe + window.print()，浏览器原生打印，最可靠
    var iframe = document.createElement('iframe');
    iframe.style.cssText = 'position:fixed; top:0; left:0; width:100%; height:100%; z-index:99999; border:none; background:white;';
    document.body.appendChild(iframe);

    var doc = iframe.contentDocument || iframe.contentWindow.document;
    doc.open();
    doc.write(
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>' + esc(customerName) + ' 售前方案</title>' +
        '<style>' +
        '@page { size: A4; margin: 15mm; }' +
        'body { font-family: "Microsoft YaHei", "SimSun", sans-serif; font-size: 13px; line-height: 1.7; color: #333; max-width: 180mm; margin: 0 auto; }' +
        'h1 { font-size: 20px; color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 8px; page-break-after: avoid; }' +
        'h2 { font-size: 16px; color: #333; margin-top: 20px; page-break-after: avoid; }' +
        'h3 { font-size: 14px; color: #444; page-break-after: avoid; }' +
        'table { width: 100%; border-collapse: collapse; font-size: 12px; page-break-inside: avoid; table-layout: fixed; }' +
        'th, td { border: 1px solid #bbb; padding: 6px 8px; text-align: left; word-break: break-word; vertical-align: top; }' +
        'th { background: #f0f4f8; font-weight: 600; }' +
        'ul, ol { padding-left: 20px; }' +
        'li { margin: 3px 0; }' +
        'p { margin: 8px 0; }' +
        'pre, code { background: #f5f5f5; padding: 2px 4px; border-radius: 3px; font-size: 11px; word-break: break-all; white-space: pre-wrap; }' +
        'blockquote { border-left: 3px solid #1a73e8; margin: 10px 0; padding-left: 12px; color: #555; }' +
        'img { max-width: 100%; }' +
        '.cover { text-align: center; margin-bottom: 30px; padding-bottom: 16px; border-bottom: 2px solid #1a73e8; }' +
        '.cover h1 { color: #1a73e8; font-size: 24px; margin: 0; border: none; }' +
        '.cover p { color: #555; font-size: 13px; margin: 4px 0 0; }' +
        '.cover .date { color: #888; font-size: 11px; margin-top: 2px; }' +
        '</style></head><body>' +
        '<div class="cover">' +
        '<h1>' + esc(customerName) + '</h1>' +
        '<p>智能售前方案报告</p>' +
        '<p class="date">' + new Date().toLocaleDateString() + '</p>' +
        '</div>' +
        '<div class="markdown-body">' + proposalEl.innerHTML + '</div>' +
        '</body></html>'
    );
    doc.close();

    // 等待样式渲染完成后打印
    setTimeout(function() {
        iframe.contentWindow.focus();
        iframe.contentWindow.print();
        // 打印对话框关闭后移除 iframe（监听焦点变化）
        var cleanup = function() {
            if (document.activeElement !== iframe) {
                if (iframe.parentNode) document.body.removeChild(iframe);
                window.removeEventListener('focus', cleanup);
            }
        };
        window.addEventListener('focus', cleanup);
    }, 300);
}

function exportMarkdown() {
    var text = resultData && resultData.proposal ? resultData.proposal : '';
    if (!text) { alert('先生成一个方案'); return; }
    var name = (document.getElementById('customerName').value || '客户') + '_售前方案.md';
    var blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    URL.revokeObjectURL(a.href);
}

// ==================== 问卷弹窗 ====================
var currentQuestionnaire = [];
var currentHistoryId = null;

function generateQuestionnaire() {
    if (!resultData || !resultData.proposal) { alert('请先生成方案'); return; }
    var btn = document.getElementById('btnQuestionnaire');
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 生成问卷...';
    fetch('/api/questionnaire', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ proposal: resultData.proposal })
    }).then(function(r) { return r.json(); }).then(function(data) {
        btn.disabled = false; btn.innerHTML = '<i class="fas fa-clipboard-list"></i> 补充问卷';
        if (data.error) { alert('问卷生成失败: ' + data.error); return; }
        currentQuestionnaire = data.questionnaire || [];
        currentHistoryId = resultData.history_id;
        if (!currentQuestionnaire.length) { alert('未识别到需要客户确认的事项'); return; }
        showQuestionnaireModal(currentQuestionnaire);
    }).catch(function(err) {
        btn.disabled = false; btn.innerHTML = '<i class="fas fa-clipboard-list"></i> 补充问卷';
        alert('请求失败: ' + err.message);
    });
}

function showQuestionnaireModal(questions) {
    var body = document.getElementById('questionnaireBody');
    body.innerHTML = '';
    questions.forEach(function(q, idx) {
        var div = document.createElement('div');
        div.className = 'q-item';
        var html = '<label class="q-label">' + (idx+1) + '. ' + esc(q.question) + '</label>';
        if (q.original_item) html += '<div class="q-desc">原方案提及：' + esc(q.original_item) + '</div>';
        var isChoice = q.type === 'choice' || q.type === 'select' || q.type === 'radio' || q.type === '单选' || q.type === '多选';
        if (isChoice && q.options && q.options.length) {
            html += '<div class="q-options">';
            q.options.forEach(function(opt) {
                html += '<label class="q-option"><input type="radio" name="' + esc(q.id) + '" value="' + esc(opt) + '"> ' + esc(opt) + '</label>';
            });
            html += '</div>';
        } else {
            html += '<textarea class="q-input" id="' + esc(q.id) + '" placeholder="请输入..." rows="2"></textarea>';
            // 添加附件上传
            html += '<div class="q-file-area">';
            html += '<label style="font-size:12px;color:#666;cursor:pointer;display:inline-flex;align-items:center;gap:4px;">';
            html += '<i class="fas fa-paperclip"></i> 添加附件';
            html += '<input type="file" id="file_' + esc(q.id) + '" style="display:none" onchange="handleFileSelect(this, \'' + esc(q.id) + '\')">';
            html += '</label>';
            html += '<span class="file-name" id="fname_' + esc(q.id) + '"></span>';
            html += '</div>';
        }
        div.innerHTML = html;
        body.appendChild(div);
    });
    // 添加自由补充信息区域
    var supplementHtml =
        '<div class="q-supplement">' +
        '<label class="q-supplement-label"><i class="fas fa-edit"></i> 其他补充信息</label>' +
        '<textarea id="supplementText" placeholder="您可以在此输入问卷未涉及的补充需求、特殊要求或其他说明..."></textarea>' +
        '<div class="q-supplement-hint">填写后将在方案更新时一并参考</div>' +
        '</div>';
    body.insertAdjacentHTML('beforeend', supplementHtml);
    document.getElementById('questionnaireModal').classList.add('active');
}

function closeQuestionnaireModal() {
    document.getElementById('questionnaireModal').classList.remove('active');
}

function handleFileSelect(input, qid) {
    var fnameSpan = document.getElementById('fname_' + qid);
    if (input.files.length > 0) {
        fnameSpan.textContent = input.files[0].name;
    } else {
        fnameSpan.textContent = '';
    }
}

function submitQuestionnaire() {
    if (!currentQuestionnaire.length || !currentHistoryId) return;
    var formData = new FormData();
    formData.append('history_id', currentHistoryId);

    var answers = {};
    currentQuestionnaire.forEach(function(q) {
        var isChoice = q.type === 'choice' || q.type === 'select' || q.type === 'radio' || q.type === '单选' || q.type === '多选';
        if (isChoice) {
            var checked = document.querySelector('input[name="' + q.id + '"]:checked');
            answers[q.id] = checked ? checked.value : '';
        } else {
            var el = document.getElementById(q.id);
            answers[q.id] = el ? el.value : '';
        }
        // 附件
        var fileInput = document.getElementById('file_' + q.id);
        if (fileInput && fileInput.files.length > 0) {
            formData.append('file_' + q.id, fileInput.files[0]);
            answers[q.id] += '\n[附件: ' + fileInput.files[0].name + ']';
        }
    });
    formData.append('answers', JSON.stringify(answers));

    // 补充信息
    var supplement = document.getElementById('supplementText');
    if (supplement && supplement.value.trim()) {
        formData.append('supplement', supplement.value.trim());
    }

    var btn = document.getElementById('btnSubmitQ');
    btn.disabled = true; btn.textContent = '更新中...';
    fetch('/api/update_proposal', {
        method: 'POST',
        body: formData
    }).then(function(r) { return r.json(); }).then(function(data) {
        btn.disabled = false; btn.textContent = '提交并更新方案';
        if (data.error) { alert('更新失败: ' + data.error); return; }
        resultData.proposal = data.proposal;
        document.getElementById('proposalOutput').innerHTML = mdToHtml(data.proposal);
        closeQuestionnaireModal();
        statusBox.innerHTML = '<div class="progress-bar"><div class="progress-fill" style="width:100%"></div></div><div class="progress-text" style="color:#34a853;font-weight:600;"><i class="fas fa-check-circle"></i> 方案已根据问卷反馈更新！</div>';
        statusBox.className = 'status-box success';
        switchTab('proposal');
    }).catch(function(err) {
        btn.disabled = false; btn.textContent = '提交并更新方案';
        alert('请求失败: ' + err.message);
    });
}

// ==================== 页面状态持久化 ====================
function saveState() {
    try {
        var state = {
            customerName: document.getElementById('customerName').value,
            customerInput: document.getElementById('customerInput').value,
            industry: document.getElementById('industry').value,
            resultData: resultData,
            activeTab: document.querySelector('.tab-btn.active')?.getAttribute('data-tab') || 'proposal',
            voiceTranscript: document.getElementById('voiceTranscript').textContent,
            voiceResultVisible: document.getElementById('voiceResult').style.display !== 'none',
            voiceStatus: document.getElementById('voiceStatus').textContent,
            currentQuestionnaire: currentQuestionnaire,
            currentHistoryId: currentHistoryId,
            proposalHtml: document.getElementById('proposalOutput').innerHTML,
            intentHtml: document.getElementById('intentOutput').innerHTML,
            competitorHtml: document.getElementById('competitorOutput').innerHTML,
            statusBoxHtml: document.getElementById('statusBox').innerHTML,
            statusBoxClass: document.getElementById('statusBox').className,
            btnQuestionnaireVisible: document.getElementById('btnQuestionnaire').style.display
        };
        sessionStorage.setItem('presalesState', JSON.stringify(state));
    } catch(e) {}
}

function restoreState() {
    try {
        var raw = sessionStorage.getItem('presalesState');
        if (!raw) return;
        var state = JSON.parse(raw);

        // 恢复表单
        if (state.customerName !== undefined) document.getElementById('customerName').value = state.customerName;
        if (state.customerInput !== undefined) document.getElementById('customerInput').value = state.customerInput;
        if (state.industry !== undefined) document.getElementById('industry').value = state.industry;

        // 恢复结果数据
        if (state.resultData) {
            resultData = state.resultData;
            if (state.proposalHtml) document.getElementById('proposalOutput').innerHTML = state.proposalHtml;
            if (state.intentHtml) document.getElementById('intentOutput').innerHTML = state.intentHtml;
            if (state.competitorHtml) document.getElementById('competitorOutput').innerHTML = state.competitorHtml;
            if (state.btnQuestionnaireVisible) document.getElementById('btnQuestionnaire').style.display = state.btnQuestionnaireVisible;
            renderProductCards();
        }

        // 恢复标签页
        if (state.activeTab) {
            switchTab(state.activeTab);
        }

        // 恢复语音转写结果
        if (state.voiceTranscript) {
            document.getElementById('voiceTranscript').textContent = state.voiceTranscript;
        }
        if (state.voiceResultVisible) {
            document.getElementById('voiceResult').style.display = 'block';
        }
        if (state.voiceStatus) {
            document.getElementById('voiceStatus').textContent = state.voiceStatus;
        }

        // 恢复问卷状态
        if (state.currentQuestionnaire) currentQuestionnaire = state.currentQuestionnaire;
        if (state.currentHistoryId) currentHistoryId = state.currentHistoryId;

        // 恢复状态栏
        if (state.statusBoxHtml) {
            document.getElementById('statusBox').innerHTML = state.statusBoxHtml;
            document.getElementById('statusBox').className = state.statusBoxClass || 'status-box';
        }

        // 恢复图表（如果在数据看板标签）
        if (state.activeTab === 'dashboard' && resultData) {
            requestAnimationFrame(function() {
                requestAnimationFrame(function() {
                    renderCharts();
                });
            });
        }
    } catch(e) {}
}

// 页面加载时恢复状态
restoreState();

// 表单变化时自动保存
document.getElementById('customerName').addEventListener('input', saveState);
document.getElementById('customerInput').addEventListener('input', saveState);
document.getElementById('industry').addEventListener('change', saveState);

window.addEventListener('resize', function() {
    Object.values(chartInstances).forEach(function(chart) {
        try { chart.resize(); } catch(e) {}
    });
});
</script>

<!-- 问卷弹窗 -->
<div class="modal-overlay" id="questionnaireModal">
  <div class="modal-box">
    <div class="modal-header">
      <h3><i class="fas fa-clipboard-list"></i> 客户补充问卷</h3>
      <button class="modal-close" onclick="closeQuestionnaireModal()">&times;</button>
    </div>
    <div class="modal-body" id="questionnaireBody">
      <!-- 动态生成问题 -->
    </div>
    <div class="modal-footer">
      <button class="btn-secondary" onclick="closeQuestionnaireModal()">稍后补充</button>
      <button class="btn-primary" id="btnSubmitQ" onclick="submitQuestionnaire()">提交并更新方案</button>
    </div>
  </div>
</div>
"""
    return render_page("首页", body, session.get("username"))


@app.route("/history")
@login_required
def history_page():
    records = _load_history(session["username"])
    records.reverse()
    rows = ""
    for r in records:
        rows += f"""
        <div class="history-item" data-id="{r['id']}">
            <div class="history-item-left">
                <input type="checkbox" class="history-checkbox" value="{r['id']}" onchange="updateBatchBtn()">
                <div class="history-item-icon"><i class="fas fa-file-alt"></i></div>
                <div class="history-item-info">
                    <h4>{r['customer_name']} - {r['industry']}</h4>
                    <div class="meta">{r['created_at']} &middot; {r['customer_input'][:50]}...</div>
                </div>
            </div>
            <div class="history-item-actions">
                <a href="/history/{r['id']}" class="btn-secondary"><i class="fas fa-eye"></i> 查看</a>
                <a href="/api/history/{r['id']}/download" class="btn-secondary"><i class="fas fa-download"></i> 下载</a>
            </div>
        </div>
        """
    if not rows:
        rows = '<p class="placeholder-text">暂无历史方案，去<a href="/" style="color:#1a73e8;">首页</a>生成一个吧</p>'

    body = f"""
    <div class="container" style="max-width:900px;">
        <div class="panel" style="width:100%;padding:20px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
                <h3 style="margin:0;"><i class="fas fa-history"></i> 历史方案 ({len(records)}条)</h3>
                <div style="display:flex;gap:8px;align-items:center;">
                    <label style="font-size:12px;color:#666;display:flex;align-items:center;gap:4px;cursor:pointer;">
                        <input type="checkbox" id="selectAll" onchange="toggleSelectAll()"> 全选
                    </label>
                    <button class="btn-secondary" id="batchDeleteBtn" onclick="batchDelete()" style="display:none;color:#cf1322;border-color:#cf1322;">
                        <i class="fas fa-trash-alt"></i> 批量删除
                    </button>
                </div>
            </div>
            <div class="history-list">{rows}</div>
        </div>
    </div>
    <script>
    function toggleSelectAll() {{
        var checked = document.getElementById('selectAll').checked;
        document.querySelectorAll('.history-checkbox').forEach(function(cb) {{ cb.checked = checked; }});
        updateBatchBtn();
    }}
    function updateBatchBtn() {{
        var any = document.querySelectorAll('.history-checkbox:checked').length > 0;
        document.getElementById('batchDeleteBtn').style.display = any ? 'inline-flex' : 'none';
    }}
    function batchDelete() {{
        var ids = Array.from(document.querySelectorAll('.history-checkbox:checked')).map(function(cb) {{ return cb.value; }});
        if (!ids.length) return;
        if (!confirm('确定要删除选中的 ' + ids.length + ' 条记录吗？此操作不可恢复。')) return;
        fetch('/api/history/batch_delete', {{
            method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ ids: ids }})
        }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
            if (data.error) {{ alert('删除失败: ' + data.error); return; }}
            ids.forEach(function(id) {{
                var el = document.querySelector('.history-item[data-id="' + id + '"]');
                if (el) el.remove();
            }});
            document.getElementById('selectAll').checked = false;
            updateBatchBtn();
            var remaining = document.querySelectorAll('.history-item').length;
            if (!remaining) {{
                document.querySelector('.history-list').innerHTML = '<p class="placeholder-text">暂无历史方案，去<a href="/" style="color:#1a73e8;">首页</a>生成一个吧</p>';
            }}
            var title = document.querySelector('h3');
            if (title) title.innerHTML = '<i class="fas fa-history"></i> 历史方案 (' + remaining + '条)';
        }}).catch(function(err) {{ alert('请求失败: ' + err.message); }});
    }}
    </script>
    """
    return render_page("历史方案", body, session.get("username"))


@app.route("/history/<record_id>")
@login_required
def history_detail(record_id):
    records = _load_history(session["username"])
    record = next((r for r in records if r["id"] == record_id), None)
    if not record:
        return "记录不存在", 404

    proposal_html = md_to_html(record["proposal"])

    body = f"""
    <div class="container" style="max-width:900px;">
        <div class="panel" style="width:100%;padding:20px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
                <h3><i class="fas fa-file-alt"></i> {record['customer_name']} - 售前方案</h3>
                <div style="display:flex;gap:6px;">
                    <a href="/api/history/{record['id']}/download" class="btn-secondary"><i class="fas fa-download"></i> 下载Markdown</a>
                    <a href="/history" class="btn-secondary"><i class="fas fa-arrow-left"></i> 返回</a>
                </div>
            </div>
            {proposal_html}
        </div>
    </div>
    """
    return render_page("方案详情", body, session.get("username"))


@app.route("/api/generate", methods=["POST"])
@login_required
def api_generate():
    def event_stream():
        try:
            data = request.get_json()
            customer_input = data.get("customer_input", "").strip()
            customer_name = data.get("customer_name", "客户")
            industry = data.get("industry", "education")

            if not customer_input:
                yield f"data: {json.dumps({'error': '客户需求不能为空'}, ensure_ascii=False)}\n\n"
                return
            if not LLM_CONFIG.get("api_key"):
                yield f"data: {json.dumps({'error': '未配置API Key'}, ensure_ascii=False)}\n\n"
                return

            llm_client = LLMClient(LLM_CONFIG)
            intent_parser = IntentParser(llm_client)
            product_matcher = ProductMatcher(llm_client, industry=industry)
            competitor_analyst = CompetitorAnalyst(llm_client)
            proposal_generator = ProposalGenerator(llm_client)

            yield f"data: {json.dumps({'progress': 5, 'status': '正在解析客户需求...'}, ensure_ascii=False)}\n\n"
            intent_result = intent_parser.parse(customer_input)
            yield f"data: {json.dumps({'progress': 25, 'status': '需求解析完成，正在匹配产品方案...'}, ensure_ascii=False)}\n\n"

            matching_result = product_matcher.match(intent_result)
            # 从原始知识库补充产品category信息到匹配结果
            try:
                kb_path = get_knowledge_base_path(industry)
                products_file = kb_path / "products.json"
                if products_file.exists():
                    with open(products_file, "r", encoding="utf-8") as f:
                        raw_products = json.load(f)
                    name_to_category = {p.get("name", ""): p.get("category", "其他") for p in raw_products}
                    for m in matching_result.get("matching_result", []):
                        pname = m.get("product_name", "")
                        if pname in name_to_category:
                            m["category"] = name_to_category[pname]
                        elif "category" not in m:
                            m["category"] = "其他"
            except Exception:
                pass
            yield f"data: {json.dumps({'progress': 50, 'status': '产品匹配完成，正在进行竞品分析...'}, ensure_ascii=False)}\n\n"

            competitor_analysis = competitor_analyst.analyze(
                customer_input=customer_input, our_solution=matching_result
            )
            yield f"data: {json.dumps({'progress': 75, 'status': '竞品分析完成，正在生成最终方案...'}, ensure_ascii=False)}\n\n"

            proposal = proposal_generator.generate(
                intent_result=intent_result, matching_result=matching_result,
                competitor_analysis=competitor_analysis, customer_name=customer_name
            )
            yield f"data: {json.dumps({'progress': 95, 'status': '方案生成完成，正在保存...'}, ensure_ascii=False)}\n\n"

            record_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            record = {
                "id": record_id,
                "customer_name": customer_name,
                "customer_input": customer_input,
                "industry": industry,
                "proposal": proposal,
                "intent_result": intent_result,
                "matching_result": matching_result,
                "competitor_analysis": competitor_analysis,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            history = _load_history(session["username"])
            history.append(record)
            _save_history(session["username"], history)

            safe_name = customer_name.replace(" ", "_").replace("/", "_")
            proposal_path = proposal_generator.save_proposal(
                proposal, filename=f"{safe_name}_售前方案_{record_id}.md"
            )

            result_payload = {
                'proposal': proposal,
                'intent_result': intent_result,
                'matching_result': matching_result,
                'competitor_analysis': competitor_analysis,
                'proposal_path': str(proposal_path),
                'history_id': record_id,
            }
            yield f"data: {json.dumps({'progress': 100, 'status': '完成', 'result': result_payload}, ensure_ascii=False)}\n\n"
        except Exception as e:
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


@app.route("/api/questionnaire", methods=["POST"])
@login_required
def api_questionnaire():
    """基于已生成的方案，提取'需要客户确认的事项'并生成结构化问卷"""
    try:
        data = request.get_json()
        proposal = data.get("proposal", "")
        if not proposal:
            return jsonify({"error": "方案内容不能为空"}), 400

        llm_client = LLMClient(LLM_CONFIG)
        prompt = f"""你是一位售前顾问。请基于以下售前方案中"需要客户确认的事项"部分，生成一份客户补充问卷。
将每个需要确认的事项转化为一个问卷问题，输出为JSON数组格式。

每个问题对象必须包含以下字段：
- id: 字符串编号，如"q1", "q2"
- question: 问题文本，简洁明了
- type: "text"(填空) 或 "choice"(单选)
- options: 如果是choice类型，提供选项数组（如["选项A","选项B","选项C"]）；text类型为空数组
- original_item: 原始确认事项的原文摘要

如果方案中没有明确的"需要客户确认的事项"，请根据方案内容推断出3-5个最关键需要客户确认的问题。

方案内容：
{proposal}

请只输出JSON数组，不要输出markdown代码块或其他说明文字。"""

        messages = [{"role": "user", "content": prompt}]
        response = llm_client.chat_completion(messages)
        questionnaire = safe_json_loads(response)
        if not isinstance(questionnaire, list):
            questionnaire = []
        return jsonify({"questionnaire": questionnaire})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/update_proposal", methods=["POST"])
@login_required
def api_update_proposal():
    """根据问卷答案，在原方案基础上增量更新"""
    try:
        # 支持 FormData（含附件）和 JSON 两种格式
        if request.content_type and 'multipart/form-data' in request.content_type:
            history_id = request.form.get("history_id")
            answers_str = request.form.get("answers", "{}")
            answers = json.loads(answers_str)
            supplement = request.form.get("supplement", "")
            # 处理附件文件信息
            file_info = []
            for key in request.files:
                f = request.files[key]
                if f and f.filename:
                    file_info.append(f.filename)
        else:
            data = request.get_json()
            history_id = data.get("history_id")
            answers = data.get("answers", {})
            supplement = data.get("supplement", "")
            file_info = []

        if not history_id:
            return jsonify({"error": "缺少历史记录ID"}), 400

        history = _load_history(session["username"])
        record = next((r for r in history if r["id"] == history_id), None)
        if not record:
            return jsonify({"error": "未找到历史记录"}), 404

        # 构建补充信息文本
        supplement_section = ""
        if supplement and supplement.strip():
            supplement_section = f"\n\n===== 客户额外补充（问卷之外的信息） =====\n{supplement.strip()}"
        if file_info:
            supplement_section += f"\n\n客户上传了以下附件文件（请参考附件内容完善方案）：{', '.join(file_info)}"

        llm_client = LLMClient(LLM_CONFIG)
        prompt = f"""你是一位资深解决方案架构师。客户已收到初步售前方案，并针对其中"需要客户确认的事项"提供了补充回答。
请基于原方案和补充信息，更新方案中的相关部分（尤其是产品配置、实施路径、预算、交付周期等），生成一份更精准的方案。

===== 原客户需求 =====
{record['customer_input']}

===== 原方案 =====
{record['proposal']}

===== 客户问卷回答 =====
{json.dumps(answers, ensure_ascii=False, indent=2)}
{supplement_section}
===== 更新要求 =====
1. 保留原方案的整体结构和章节
2. 仅修改与补充信息相关的部分（产品配置、数量、预算、实施计划等）
3. 在修改过的段落开头标注【已根据客户反馈更新】
4. 输出完整的更新后方案Markdown文本
5. 如果补充信息不足，保留原内容不变"""

        messages = [{"role": "user", "content": prompt}]
        updated_proposal = llm_client.chat_completion(messages)

        record["proposal"] = updated_proposal
        record["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record["questionnaire_answers"] = answers
        _save_history(session["username"], history)

        return jsonify({
            "proposal": updated_proposal,
            "history_id": history_id,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/transcribe", methods=["POST"])
@login_required
def api_transcribe():
    """上传音频文件，调用讯飞ASR转写为文字"""
    try:
        if not XFYUN_ASR_CONFIG.get("app_id"):
            return jsonify({"error": "讯飞ASR未配置，请设置环境变量 XFYUN_ASR_APP_ID/API_KEY/API_SECRET"}), 500

        if "audio" not in request.files:
            return jsonify({"error": "未上传音频文件"}), 400

        audio_file = request.files["audio"]
        if not audio_file or not audio_file.filename:
            return jsonify({"error": "音频文件为空"}), 400

        # 保存上传文件到临时目录
        temp_dir = Path(__file__).parent.parent / "data" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        input_path = temp_dir / audio_file.filename
        audio_file.save(str(input_path))

        # ffmpeg 转换为 PCM 16kHz 16bit 单声道
        pcm_path = str(input_path) + ".pcm"
        convert_audio_to_pcm(str(input_path), pcm_path)

        # 读取 PCM 数据
        with open(pcm_path, "rb") as f:
            pcm_bytes = f.read()

        # 调用讯飞ASR
        asr = XfyunASRClient(
            app_id=XFYUN_ASR_CONFIG["app_id"],
            api_key=XFYUN_ASR_CONFIG["api_key"],
            api_secret=XFYUN_ASR_CONFIG["api_secret"],
        )
        text = asr.transcribe(pcm_bytes)

        # 清理临时文件
        try:
            os.remove(str(input_path))
            os.remove(pcm_path)
        except Exception:
            pass

        return jsonify({"text": text})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/extract_needs", methods=["POST"])
@login_required
def api_extract_needs():
    """接收转写文本，由大模型提炼结构化客户需求"""
    try:
        data = request.get_json()
        transcript = data.get("transcript", "").strip()
        if not transcript:
            return jsonify({"error": "转写文本为空"}), 400

        llm_client = LLMClient(LLM_CONFIG)
        prompt = f"""你是一位资深售前顾问。以下是一段客户会议录音的转写文本，请从中提炼出结构化的客户需求信息。

===== 会议录音转写文本 =====
{transcript}

===== 提炼要求 =====
请从上述文本中提取以下字段，以JSON格式输出：
{{
  "customer_name": "客户名称（如未提及请返回\"\"）",
  "industry": "行业领域，只能从以下选项中选择一个：education（教育）、healthcare（医疗）、manufacturing（制造）、finance（金融）。如无法判断请返回\"education\"",
  "customer_input": "客户需求描述（将会议中提到的所有需求整理成一段完整、通顺的文字，包含预算、时间、功能要求等关键信息。如未提及某项请忽略，不要编造）"
}}

注意：
1. 只输出JSON，不要输出任何解释文字
2. 如某个字段信息不足，用空字符串或默认值填充
3. customer_input 要尽可能详细和完整"""

        messages = [{"role": "user", "content": prompt}]
        response = llm_client.chat_completion(messages, temperature=0.1, max_tokens=2048)

        # 提取JSON
        extracted = safe_json_loads(response)
        if not isinstance(extracted, dict):
            extracted = {"customer_name": "", "industry": "education", "customer_input": transcript}

        return jsonify({
            "extracted": extracted,
            "transcript": transcript,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/transcribe_file", methods=["POST"])
@login_required
def api_transcribe_file():
    """上传音频文件，使用讯飞录音文件转写（支持长音频、MP3等格式）"""
    try:
        if not XFYUN_LFASR_CONFIG.get("app_id"):
            return jsonify({"error": "讯飞录音文件转写未配置，请设置环境变量 XFYUN_LFASR_APP_ID/SECRET_KEY"}), 500

        if "audio" not in request.files:
            return jsonify({"error": "未上传音频文件"}), 400

        audio_file = request.files["audio"]
        if not audio_file or not audio_file.filename:
            return jsonify({"error": "音频文件为空"}), 400

        # 保存上传文件到临时目录
        temp_dir = Path(__file__).parent.parent / "data" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        input_path = temp_dir / audio_file.filename
        audio_file.save(str(input_path))

        task_id = str(uuid.uuid4())
        with _lfasr_lock:
            _lfasr_tasks[task_id] = {"status": "uploading", "text": "", "error": ""}

        def do_transcribe():
            try:
                client = XfyunLFASRClient(
                    app_id=XFYUN_LFASR_CONFIG["app_id"],
                    secret_key=XFYUN_LFASR_CONFIG["secret_key"],
                )

                def progress_cb(status, desc):
                    with _lfasr_lock:
                        _lfasr_tasks[task_id]["status"] = f"{status}: {desc}"

                text = client.transcribe(str(input_path), progress_callback=progress_cb)
                with _lfasr_lock:
                    _lfasr_tasks[task_id]["status"] = "done"
                    _lfasr_tasks[task_id]["text"] = text
            except Exception as e:
                traceback.print_exc()
                with _lfasr_lock:
                    _lfasr_tasks[task_id]["status"] = "error"
                    _lfasr_tasks[task_id]["error"] = str(e)
            finally:
                try:
                    os.remove(str(input_path))
                except Exception:
                    pass

        thread = threading.Thread(target=do_transcribe, daemon=True)
        thread.start()

        return jsonify({"task_id": task_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/transcribe_file/progress/<task_id>")
@login_required
def api_transcribe_file_progress(task_id):
    """查询录音文件转写进度"""
    with _lfasr_lock:
        task = _lfasr_tasks.get(task_id, {})
    return jsonify({
        "status": task.get("status", "unknown"),
        "text": task.get("text", ""),
        "error": task.get("error", ""),
    })


@app.route("/api/history/<record_id>/download")
@login_required
def download_history(record_id):
    records = _load_history(session["username"])
    record = next((r for r in records if r["id"] == record_id), None)
    if not record:
        return "记录不存在", 404
    from io import BytesIO
    blob = BytesIO(record["proposal"].encode("utf-8"))
    safe_name = record["customer_name"].replace(" ", "_").replace("/", "_")
    return (blob.read(), {
        "Content-Type": "text/markdown; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{safe_name}_售前方案.md"'
    })


@app.route("/api/history/batch_delete", methods=["POST"])
@login_required
def batch_delete_history():
    """批量删除历史记录"""
    try:
        data = request.get_json()
        ids = data.get("ids", [])
        if not ids or not isinstance(ids, list):
            return jsonify({"error": "未选择记录"}), 400

        history = _load_history(session["username"])
        original_count = len(history)
        history = [r for r in history if r["id"] not in ids]
        deleted_count = original_count - len(history)
        _save_history(session["username"], history)

        return jsonify({"deleted": deleted_count, "message": f"已删除 {deleted_count} 条记录"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("=" * 55)
    print("  B端智能售前方案生成系统 v3.2")
    print("  Bugfix: 登录/渲染/PDF/表格/图表")
    print("=" * 55)
    print("  http://localhost:5000")
    print("  默认: admin / admin123")
    print("=" * 55)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)