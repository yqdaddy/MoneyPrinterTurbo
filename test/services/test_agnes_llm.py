"""Agnes AI 接入回归：使用真实 SDK 编解码，HTTP 层不访问外网或消耗额度。"""

import json
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from app.config import config
from app.models.llm_provider import get_llm_provider
from app.services import llm


@pytest.mark.parametrize(
    "base_url,model,expected_url,expected_model",
    [
        ("", "", "https://api.agnes-ai.cn/v1/chat/completions", "agnes-3.0-flash"),
        ("  ", "  ", "https://api.agnes-ai.cn/v1/chat/completions", "agnes-3.0-flash"),
        (
            "https://gateway.example.com/custom/v1/",
            "agnes-3.0-pro",
            "https://gateway.example.com/custom/v1/chat/completions",
            "agnes-3.0-pro",
        ),
    ],
)
def test_agnes_defaults_and_model_overrides(
    base_url, model, expected_url, expected_model
):
    """空配置使用默认值，自定义值不被覆盖；其他 Provider 的配置保持原样。"""
    snapshot = dict(config.app)
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == expected_url
        assert request.headers["Authorization"] == "Bearer test-agnes-key"
        assert json.loads(request.content) == {
            "model": expected_model,
            "messages": [{"role": "user", "content": "生成中文文案"}],
        }
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": expected_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "你好，世界！"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    # 保留真实 SDK 的 URL 拼接和响应解析，只替换 HTTP 传输层，避免 mock 掩盖协议问题。
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        with OpenAI(
            api_key="test-agnes-key",
            base_url=base_url.strip() or "https://api.agnes-ai.cn/v1",
            http_client=http_client,
            max_retries=0,
        ) as client:
            with patch.object(llm, "OpenAI", return_value=client) as factory:
                result = llm._generate_response(
                    "生成中文文案",
                    app_config={
                        "llm_provider": "agnes",
                        "agnes_api_key": "test-agnes-key",
                        "agnes_base_url": base_url,
                        "agnes_model_name": model,
                    },
                )
                factory.assert_called_once_with(
                    api_key="test-agnes-key",
                    base_url=base_url.strip() or "https://api.agnes-ai.cn/v1",
                )
    assert result == "你好，世界！"
    assert len(requests) == 1
    assert config.app == snapshot


def test_agnes_missing_key_fails_before_network():
    """缺少密钥时沿用统一错误提示，不创建客户端或发起付费请求。"""
    with patch.object(llm, "OpenAI") as factory:
        result = llm._generate_response("test", app_config={"llm_provider": "agnes"})
    assert result.startswith("Error:")
    assert "agnes: api_key is not set" in result
    factory.assert_not_called()


def test_agnes_registry_metadata():
    """锁定默认模型和地址，避免不同入口的信息不一致。"""
    provider = get_llm_provider("agnes")
    assert provider is not None
    assert provider.default_label == "Agnes AI"
    assert provider.default_model == "agnes-3.0-flash"
    assert provider.default_base_url == "https://api.agnes-ai.cn/v1"
    assert provider.adapter == "openai_compatible"
    assert provider.requires_api_key
