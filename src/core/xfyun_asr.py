"""
讯飞语音听写（流式版）WebSocket 客户端
文档：https://www.xfyun.cn/doc/asr/voicedictation/API.html
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import wave
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import websockets


class XfyunASRClient:
    """讯飞语音听写客户端"""

    HOST = "iat-api.xfyun.cn"
    PATH = "/v2/iat"
    URL = f"wss://{HOST}{PATH}"

    def __init__(self, app_id: str, api_key: str, api_secret: str):
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret

    def _build_auth_url(self) -> str:
        """生成带鉴权参数的 WebSocket URL（HMAC-SHA256）"""
        date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        signature_origin = f"host: {self.HOST}\ndate: {date}\nGET {self.PATH} HTTP/1.1"
        signature_sha = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.b64encode(signature_sha).decode("utf-8")
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
        params = {"host": self.HOST, "date": date, "authorization": authorization}
        return f"{self.URL}?{urlencode(params)}"

    async def _send_audio(self, websocket, pcm_bytes: bytes):
        """分帧发送音频数据"""
        frame_size = 1280  # 40ms @ 16kHz 16bit mono
        total = len(pcm_bytes)
        for i in range(0, total, frame_size):
            chunk = pcm_bytes[i : i + frame_size]
            status = 2 if i + frame_size >= total else 1
            if i == 0:
                status = 0  # 第一帧
            msg = {
                "data": {
                    "status": status,
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(chunk).decode("utf-8"),
                }
            }
            # 首帧带公共和业务参数
            if status == 0:
                msg["common"] = {"app_id": self.app_id}
                msg["business"] = {
                    "language": "zh_cn",
                    "domain": "iat",
                    "accent": "mandarin",
                    "ptt": 1,
                    "dwa": "wpgs",  # 开启动态修正
                }
            await websocket.send(json.dumps(msg))
            await asyncio.sleep(0.04)  # 40ms 间隔模拟实时流

    async def _receive_results(self, websocket) -> str:
        """接收转写结果并合并"""
        results = {}
        async for message in websocket:
            try:
                resp = json.loads(message)
            except json.JSONDecodeError:
                continue
            code = resp.get("code")
            if code != 0:
                err_msg = resp.get("message", "未知错误")
                raise RuntimeError(f"讯飞ASR错误 [{code}]: {err_msg}")
            data = resp.get("data", {})
            if not data:
                continue
            result = data.get("result", {})
            ws = result.get("ws", [])
            if not ws:
                continue
            # 提取文字
            text = "".join(
                cw.get("w", "") for item in ws for cw in item.get("cw", [])
            )
            sn = result.get("sn", 0)
            ls = result.get("ls", False)
            rg = result.get("rg", [sn, sn + 1])
            # 动态修正：用最新结果覆盖
            if "wpgs" in str(data) or result.get("pgs") == "rpl":
                for i in range(rg[0], rg[1]):
                    results[i] = ""
            results[sn] = text
            if ls:
                break
        # 按序号排序拼接
        sorted_text = ""
        for i in sorted(results.keys()):
            sorted_text += results[i]
        return sorted_text

    async def transcribe_async(self, pcm_bytes: bytes) -> str:
        """异步转写 PCM 音频"""
        url = self._build_auth_url()
        async with websockets.connect(url) as ws:
            sender = asyncio.create_task(self._send_audio(ws, pcm_bytes))
            receiver = asyncio.create_task(self._receive_results(ws))
            done, pending = await asyncio.wait(
                [sender, receiver], return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
            return receiver.result()

    def transcribe(self, pcm_bytes: bytes) -> str:
        """同步入口：转写 PCM 音频（16kHz 16bit 单声道）"""
        return asyncio.run(self.transcribe_async(pcm_bytes))


def convert_audio_to_pcm(input_path: str, output_path: str = None) -> str:
    """
    将音频转换为 PCM 16kHz 16bit 单声道。
    优先使用 Python 内置 wave + numpy/scipy 处理 WAV 格式（无需 ffmpeg），
    失败时回退到 ffmpeg。
    返回输出文件路径
    """
    if output_path is None:
        output_path = input_path + ".pcm"

    import wave
    import numpy as np

    # 尝试用 Python 处理 WAV 格式
    try:
        with wave.open(input_path, "rb") as wf:
            nchannels = wf.getnchannels()
            framerate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            nframes = wf.getnframes()
            audio_bytes = wf.readframes(nframes)

        # 根据采样宽度解析为 numpy 数组
        if sampwidth == 1:
            audio = np.frombuffer(audio_bytes, dtype=np.uint8).astype(np.int16)
            audio = (audio - 128) * 256
        elif sampwidth == 2:
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
        elif sampwidth == 3:
            # 24bit 处理
            raw = np.frombuffer(audio_bytes, dtype=np.uint8).reshape(-1, 3)
            audio = ((raw[:, 2].astype(np.int32) << 16) |
                     (raw[:, 1].astype(np.int32) << 8) |
                     raw[:, 0].astype(np.int32))
            audio = audio.astype(np.int16)
        elif sampwidth == 4:
            audio = np.frombuffer(audio_bytes, dtype=np.int32).astype(np.int16)
        else:
            raise ValueError(f"不支持的采样宽度: {sampwidth}")

        # 立体声转单声道
        if nchannels > 1:
            audio = audio.reshape(-1, nchannels).mean(axis=1).astype(np.int16)

        # 重采样到 16kHz
        if framerate != 16000:
            from scipy import signal
            num_samples = int(len(audio) * 16000 / framerate)
            audio = signal.resample(audio, num_samples).astype(np.int16)

        with open(output_path, "wb") as f:
            f.write(audio.tobytes())
        return output_path
    except Exception as py_err:
        # WAV 处理失败，回退到 ffmpeg
        import subprocess
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-ar", "16000", "-ac", "1", "-f", "s16le", output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"音频转换失败。Python wave 处理: {py_err}; ffmpeg: {result.stderr}"
            )
        return output_path
