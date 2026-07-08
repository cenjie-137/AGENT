"""
LLM客户端抽象层
支持多模型切换：OpenAI / 讯飞星火 / DeepSeek
"""
import json
import os
from typing import Dict, List, Optional

import requests


class LLMClient:
    """统一LLM调用接口"""

    def __init__(self, config: Dict):
        self.provider = config.get("provider", "openai")
        self.api_key = config.get("api_key", "")
        self.api_base = config.get("api_base", "")
        self.model = config.get("model", "gpt-4o")
        self.temperature = config.get("temperature", 0.3)
        self.max_tokens = config.get("max_tokens", 4096)

        # 讯飞星火特有配置
        self.spark_config = config.get("spark", {})

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
    ) -> str:
        """统一对话接口"""
        if self.provider == "openai":
            return self._call_openai(messages, temperature, max_tokens, response_format)
        elif self.provider == "spark":
            return self._call_spark(messages, temperature, max_tokens)
        elif self.provider == "deepseek":
            return self._call_deepseek(messages, temperature, max_tokens)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    def _call_openai(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_format: Optional[Dict],
    ) -> str:
        """调用OpenAI兼容API"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        base_url = self.api_base or "https://api.openai.com/v1"
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_spark(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        """调用讯飞星火API (WebSocket或HTTP版)"""
        # TODO: 实现讯飞星火API调用
        # 讯飞星火使用WebSocket协议，需要单独实现鉴权和连接
        raise NotImplementedError("讯飞星火API调用需要单独实现WebSocket鉴权")

    def _call_deepseek(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        """调用DeepSeek API (OpenAI兼容格式)"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        base_url = self.api_base or "https://api.deepseek.com/v1"
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
