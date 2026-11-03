"""Agnes 文生图素材源的契约测试。

Agnes Image 2.5 Flash 走 OpenAI 兼容的 /images/generations 端点，但请求
体语义不同：``size`` 是清晰度档位（1K-4K）而非像素尺寸，画幅由 ``ratio``
表达，且文档未定义 ``n`` 参数。这些测试锁定 Agnes 专属的请求契约与计费
安全复用（按需生成、凑够即停、key 缺失先于付费请求失败）。
"""

import base64
import io
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoParams
from app.services import material, state as sm
from app.services import task as task_service


AGNES_IMAGE_DEFAULT_BASE_URL = "https://api.agnes-ai.cn/v1"


def _png_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color=(12, 168, 64))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _image_response(payload, status_code=200):
    return SimpleNamespace(status_code=status_code, headers={}, json=lambda: payload)


def _b64_payload(png: bytes) -> dict:
    return {
        "created": 1730000000,
        "data": [
            {
                "b64_json": base64.b64encode(png).decode("ascii"),
                "revised_prompt": "sunrise over mountains",
            }
        ],
    }


def _download_response(content: bytes):
    return SimpleNamespace(
        status_code=200,
        headers={"Content-Length": str(len(content))},
        iter_content=lambda chunk_size: iter([content]),
        close=lambda: None,
    )


@pytest.fixture(autouse=True)
def restore_config():
    original_app = dict(config.app)
    original_proxy = dict(config.proxy)
    config.app["agnes_image_api_keys"] = "agnes-image-key"
    config.proxy.clear()
    yield
    config.app.clear()
    config.app.update(original_app)
    config.proxy.clear()
    config.proxy.update(original_proxy)


def test_generate_images_agnes_sends_tier_size_and_ratio_payload():
    png = _png_bytes(8, 12)
    response = _image_response(_b64_payload(png))
    with tempfile.TemporaryDirectory() as save_dir:
        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            items = material.generate_images_agnes(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
                save_dir=save_dir,
            )
        saved_on_disk = os.path.isfile(items[0].url)

    assert post.call_args.args[0] == (
        f"{AGNES_IMAGE_DEFAULT_BASE_URL}/images/generations"
    )
    # Agnes 文档未定义 n 参数，一次请求一张图，不得发送。
    assert post.call_args.kwargs["json"] == {
        "model": "agnes-image-2.5-flash",
        "prompt": "sunrise over mountains",
        "size": "2K",
        "ratio": "9:16",
    }
    assert post.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer agnes-image-key"
    )

    assert len(items) == 1
    item = items[0]
    assert item.provider == "agnes_image"
    assert item.duration == 5
    assert item.url.startswith(save_dir)
    assert saved_on_disk
    assert item.source_info["provider"] == "agnes_image"
    assert item.source_info["search_term"] == "sunrise over mountains"
    # rendition 记录图片真实解码尺寸，而不是请求档位。
    assert item.source_info["rendition"] == {"id": None, "width": 8, "height": 12}


@pytest.mark.parametrize(
    "aspect,ratio",
    [
        (VideoAspect.portrait, "9:16"),
        (VideoAspect.landscape, "16:9"),
        (VideoAspect.square, "1:1"),
    ],
)
def test_generate_images_agnes_ratio_follows_video_aspect(aspect, ratio):
    config.app["agnes_image_size"] = "3K"
    config.app["agnes_image_base_url"] = "https://agnes.example.test/v1/"
    config.app["agnes_image_model"] = "agnes-image-2.5-pro"
    png = _png_bytes(8, 8)
    response = _image_response(_b64_payload(png))
    with tempfile.TemporaryDirectory() as save_dir:
        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_agnes(
                "city lights", minimum_duration=5, video_aspect=aspect, save_dir=save_dir
            )

    assert post.call_args.args[0] == "https://agnes.example.test/v1/images/generations"
    assert post.call_args.kwargs["json"] == {
        "model": "agnes-image-2.5-pro",
        "prompt": "city lights",
        "size": "3K",
        "ratio": ratio,
    }


def test_generate_images_agnes_url_response_is_downloaded():
    png = _png_bytes(4, 4)
    response = _image_response(
        {"created": 1, "data": [{"url": "https://cdn.agnes.example/image.png"}]}
    )
    download = _download_response(png)
    with tempfile.TemporaryDirectory() as save_dir:
        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch(
                "app.services.material.requests.get", return_value=download
            ) as get,
        ):
            items = material.generate_images_agnes(
                "quiet lake", minimum_duration=5, save_dir=save_dir
            )
        saved_on_disk = os.path.isfile(items[0].url)

    assert get.call_args.args[0] == "https://cdn.agnes.example/image.png"
    assert post.call_count == 1
    assert len(items) == 1
    assert saved_on_disk


def test_generate_images_agnes_requires_api_key_before_paid_request():
    config.app["agnes_image_api_keys"] = ""
    with patch("app.services.material.requests.post") as post:
        with pytest.raises(ValueError, match="agnes_image_api_keys"):
            material.generate_images_agnes("sunrise", minimum_duration=5)
    post.assert_not_called()


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("", False),
        ("agnes-image-key", True),
        ([], False),
        (["", "  "], False),
        (["", "agnes-backup-key"], True),
    ],
)
def test_is_agnes_image_enabled_requires_api_key(keys, expected):
    config.app["agnes_image_api_keys"] = keys
    assert material.is_agnes_image_enabled(config.app) is expected


def test_download_videos_dispatches_agnes_image_to_on_demand_wrapper():
    with patch.object(
        material, "_download_videos_openai_image_on_demand"
    ) as on_demand:
        material.download_videos(
            task_id="agnes-img-task",
            search_terms=["first scene"],
            source="agnes_image",
            audio_duration=10,
            max_clip_duration=5,
        )

    kwargs = on_demand.call_args.kwargs
    assert kwargs["image_generator"] is material.generate_images_agnes
    assert kwargs["provider"] == "agnes_image"
    assert kwargs["task_id"] == "agnes-img-task"
    assert kwargs["search_terms"] == ["first scene"]
    assert kwargs["audio_duration"] == 10
    assert kwargs["max_clip_duration"] == 5


def test_task_preflight_rejects_agnes_image_without_key():
    params = VideoParams(video_subject="Agnes image", video_source="agnes_image")
    memory_state = sm.MemoryState()
    with (
        patch.object(
            task_service.material, "is_agnes_image_enabled", return_value=False
        ),
        patch.object(task_service.sm, "state", memory_state),
        patch.object(task_service, "generate_script") as generate_script,
    ):
        result = task_service.start("agnes-image-preflight", params, stop_at="materials")

    assert result["failed_stage"] == "preflight"
    assert "Agnes image generation requires" in result["error"]
    generate_script.assert_not_called()
