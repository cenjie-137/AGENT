"""
讯飞录音文件转写（长音频）REST API 客户端
文档：https://www.xfyun.cn/doc/asr/lfasr/API.html
支持音频时长：5小时以内，文件大小：500M以内
音频格式：wav/flac/opus/mp3/m4a
"""
import base64
import hashlib
import hmac
import json
import os
import time
import traceback
from pathlib import Path
from typing import Optional

import requests


class SliceIdGenerator:
    """分片ID生成器"""

    def __init__(self):
        self._ch = "aaaaaaaaa`"

    def get_next_slice_id(self) -> str:
        ch = self._ch
        j = len(ch) - 1
        while j >= 0:
            cj = ch[j]
            if cj != "z":
                ch = ch[:j] + chr(ord(cj) + 1) + ch[j + 1 :]
                break
            else:
                ch = ch[:j] + "a" + ch[j + 1 :]
                j = j - 1
        self._ch = ch
        return self._ch


class XfyunLFASRClient:
    """讯飞录音文件转写客户端"""

    BASE_URL = "https://raasr.xfyun.cn/api"
    SLICE_SIZE = 10 * 1024 * 1024  # 10MB 分片大小

    def __init__(self, app_id: str, secret_key: str):
        self.app_id = app_id
        self.secret_key = secret_key

    def _generate_signa(self, ts: str) -> str:
        """生成签名 signa = base64(HmacSHA1(MD5(appid + ts), secretkey))"""
        base_string = self.app_id + ts
        md5_hash = hashlib.md5(base_string.encode("utf-8")).hexdigest()
        hmac_sha1 = hmac.new(
            self.secret_key.encode("utf-8"),
            md5_hash.encode("utf-8"),
            digestmod=hashlib.sha1,
        ).digest()
        signa = base64.b64encode(hmac_sha1).decode("utf-8")
        return signa

    def _get_auth_params(self) -> dict:
        """获取鉴权参数"""
        ts = str(int(time.time()))
        signa = self._generate_signa(ts)
        return {"app_id": self.app_id, "signa": signa, "ts": ts}

    def _post(self, endpoint: str, data: dict, files: dict = None) -> dict:
        """发送POST请求"""
        url = f"{self.BASE_URL}{endpoint}"
        resp = requests.post(url, data=data, files=files, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def prepare(
        self,
        file_path: str,
        language: str = "cn",
        pd: Optional[str] = None,
    ) -> str:
        """
        预处理接口，返回 task_id
        """
        file_path = Path(file_path)
        file_len = file_path.stat().st_size
        file_name = file_path.name
        slice_num = max(1, (file_len + self.SLICE_SIZE - 1) // self.SLICE_SIZE)

        data = self._get_auth_params()
        data.update(
            {
                "file_len": str(file_len),
                "file_name": file_name,
                "slice_num": str(slice_num),
                "language": language,
                "lfasr_type": "0",
            }
        )
        if pd:
            data["pd"] = pd

        result = self._post("/prepare", data)
        if result.get("ok") != 0:
            raise RuntimeError(
                f"预处理失败 [{result.get('err_no')}]: {result.get('failed')}"
            )
        return result["data"]

    def upload_slice(self, task_id: str, slice_id: str, slice_bytes: bytes) -> None:
        """上传单个分片"""
        data = self._get_auth_params()
        data["task_id"] = task_id
        data["slice_id"] = slice_id

        files = {"content": ("slice", slice_bytes)}
        result = self._post("/upload", data, files=files)
        if result.get("ok") != 0:
            raise RuntimeError(
                f"上传分片失败 [{result.get('err_no')}]: {result.get('failed')}"
            )

    def upload_file(self, task_id: str, file_path: str) -> None:
        """上传完整文件（自动分片）"""
        file_path = Path(file_path)
        file_len = file_path.stat().st_size
        slice_gen = SliceIdGenerator()

        with open(file_path, "rb") as f:
            offset = 0
            while offset < file_len:
                chunk = f.read(self.SLICE_SIZE)
                slice_id = slice_gen.get_next_slice_id()
                self.upload_slice(task_id, slice_id, chunk)
                offset += len(chunk)

    def merge(self, task_id: str) -> None:
        """合并文件，通知服务端开始转写"""
        data = self._get_auth_params()
        data["task_id"] = task_id
        result = self._post("/merge", data)
        if result.get("ok") != 0:
            raise RuntimeError(
                f"合并文件失败 [{result.get('err_no')}]: {result.get('failed')}"
            )

    def get_progress(self, task_id: str) -> dict:
        """查询处理进度"""
        data = self._get_auth_params()
        data["task_id"] = task_id
        result = self._post("/getProgress", data)
        if result.get("ok") != 0:
            raise RuntimeError(
                f"查询进度失败 [{result.get('err_no')}]: {result.get('failed')}"
            )
        # data 是 JSON 字符串，如 {"desc":"任务创建成功","status":0}
        progress_info = json.loads(result.get("data", "{}"))
        return progress_info

    def get_result(self, task_id: str) -> list:
        """获取转写结果"""
        data = self._get_auth_params()
        data["task_id"] = task_id
        result = self._post("/getResult", data)
        if result.get("ok") != 0:
            raise RuntimeError(
                f"获取结果失败 [{result.get('err_no')}]: {result.get('failed')}"
            )
        # data 是 JSON 字符串，如 [{"bg":"0","ed":"4950","onebest":"...","speaker":"0"}, ...]
        return json.loads(result.get("data", "[]"))

    def transcribe(
        self,
        file_path: str,
        language: str = "cn",
        pd: Optional[str] = None,
        progress_callback=None,
    ) -> str:
        """
        完整的转写流程：上传 → 转写 → 轮询 → 返回文本
        progress_callback: 可选的进度回调函数，接收 (status, desc) 参数
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 1. 预处理
        if progress_callback:
            progress_callback(0, "正在预处理...")
        task_id = self.prepare(str(file_path), language=language, pd=pd)

        # 2. 上传文件
        if progress_callback:
            progress_callback(1, "正在上传文件...")
        self.upload_file(task_id, str(file_path))

        # 3. 合并文件
        if progress_callback:
            progress_callback(2, "正在开始转写...")
        self.merge(task_id)

        # 4. 轮询进度（status=9 表示完成）
        if progress_callback:
            progress_callback(3, "正在转写中，请稍候...")

        status = -1
        retry_count = 0
        max_retries = 360  # 最多轮询360次，每次10秒，共1小时

        while status != 9 and retry_count < max_retries:
            time.sleep(10)
            progress_info = self.get_progress(task_id)
            status = progress_info.get("status", -1)
            desc = progress_info.get("desc", "处理中...")
            if progress_callback:
                progress_callback(status, desc)
            retry_count += 1

        if status != 9:
            raise RuntimeError(f"转写超时，最终状态: {status}")

        # 5. 获取结果
        if progress_callback:
            progress_callback(9, "正在获取结果...")
        results = self.get_result(task_id)

        # 合并文本
        texts = [item.get("onebest", "") for item in results if "onebest" in item]
        return "\n".join(texts)
