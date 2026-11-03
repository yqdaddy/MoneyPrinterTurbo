import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoParams
from app.services import agnes_video
from app.services import material, state as sm
from app.services import task as task_service


def _response(payload, status_code=200, headers=None):
    return SimpleNamespace(
        status_code=status_code,
        headers=headers or {},
        json=lambda: payload,
    )


@pytest.fixture(autouse=True)
def restore_config():
    original_app = dict(config.app)
    original_proxy = dict(config.proxy)
    config.app.update(
        {
            "agnes_video_api_key": "agnes-test-key",
            "agnes_video_base_url": agnes_video.DEFAULT_BASE_URL,
            "agnes_video_model_name": agnes_video.DEFAULT_MODEL,
            "agnes_video_poll_interval": 5,
            "agnes_video_run_timeout": 1800,
        }
    )
    config.proxy.clear()
    yield
    config.app.clear()
    config.app.update(original_app)
    config.proxy.clear()
    config.proxy.update(original_proxy)


def test_api_key_prefers_config_then_agnes_environment():
    with patch.dict(os.environ, {"AGNES_VIDEO_API_KEY": "environment-key"}, clear=False):
        assert agnes_video.get_api_key() == "agnes-test-key"
        config.app["agnes_video_api_key"] = ""
        assert agnes_video.get_api_key() == "environment-key"
        os.environ["AGNES_VIDEO_API_KEY"] = ""
        assert agnes_video.get_api_key() == ""


@pytest.mark.parametrize("term", ["", "   ", None])
def test_invalid_input_fails_before_paid_submission(term):
    with patch.object(agnes_video.requests, "post") as post:
        with pytest.raises(agnes_video.AgnesVideoError):
            agnes_video.generate_videos(term, 5)
    post.assert_not_called()


def test_submit_poll_and_parse_successful_video():
    submit = _response({"video_id": "agnes-video-1", "status": "queued"}, 200)
    polls = [
        _response({"video_id": "agnes-video-1", "status": "processing"}),
        _response(
            {
                "video_id": "agnes-video-1",
                "status": "completed",
                "url": "https://cdn.example.com/video.mp4",
            }
        ),
    ]
    with (
        patch.object(agnes_video.requests, "post", return_value=submit) as post,
        patch.object(agnes_video.requests, "get", side_effect=polls) as get,
        patch.object(agnes_video.time, "sleep") as sleep,
    ):
        result = agnes_video.generate_videos(
            "  sunrise over mountains  ",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

    assert len(result) == 1
    item = result[0]
    assert item.provider == "agnes_video"
    assert item.url == "https://cdn.example.com/video.mp4"
    assert item.duration == 5
    assert item.source_info == {
        "provider": "agnes_video",
        "search_term": "sunrise over mountains",
        "asset_id": "agnes-video-1",
        "rendition": {
            "id": "agnes-video-1",
            "width": 1080,
            "height": 1920,
        },
    }
    assert post.call_args.args[0] == f"{agnes_video.DEFAULT_BASE_URL}/videos"
    assert post.call_args.kwargs["headers"] == {
        "Authorization": "Bearer agnes-test-key",
        "Content-Type": "application/json",
    }
    assert post.call_args.kwargs["allow_redirects"] is False
    assert post.call_args.kwargs["json"] == {
        "model": "agnes-video-2.5-flash",
        "prompt": "sunrise over mountains",
        "mode": "text",
        "seconds": "5",
        "size": "720P",
        "aspect_ratio": "9:16",
    }
    assert get.call_count == 2
    assert all(
        call.args[0]
        == (
            "https://api.agnes-ai.cn/agnesapi"
            "?video_id=agnes-video-1&model_name=agnes-video-2.5-flash"
        )
        for call in get.call_args_list
    )
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)
    sleep.assert_called_once_with(agnes_video.DEFAULT_POLL_INTERVAL_SECONDS)


def test_configured_base_url_and_duration_bounds_are_applied():
    config.app.update(
        {
            "agnes_video_base_url": "https://agnes.example.test/v1/",
            "agnes_video_model_name": "agnes-video-2.5-pro",
            "agnes_video_min_duration": 5,
            "agnes_video_max_duration": 8,
        }
    )
    submit = _response({"video_id": "agnes-bounds"}, 200)
    completed = _response(
        {
            "video_id": "agnes-bounds",
            "status": "completed",
            "url": "https://cdn.example.com/bounds.mp4",
        }
    )
    with (
        patch.object(agnes_video.requests, "post", return_value=submit) as post,
        patch.object(agnes_video.requests, "get", return_value=completed),
    ):
        result = agnes_video.generate_videos("city", 99, VideoAspect.landscape)

    assert result[0].duration == 8
    assert post.call_args.args[0] == "https://agnes.example.test/v1/videos"
    assert post.call_args.kwargs["json"] == {
        "model": "agnes-video-2.5-pro",
        "prompt": "city",
        "mode": "text",
        "seconds": "8",
        "size": "720P",
        "aspect_ratio": "16:9",
    }


def test_submission_errors_are_not_retried_and_secret_is_redacted():
    rejected = _response({"detail": "rejected agnes-test-key"}, status_code=400)
    with patch.object(agnes_video.requests, "post", return_value=rejected) as post:
        with pytest.raises(agnes_video.AgnesVideoError) as raised:
            agnes_video.generate_videos("unsafe", 5)
    assert "agnes-test-key" not in str(raised.value)
    assert post.call_count == 1

    for side_effect, response in (
        (requests.exceptions.ConnectionError("offline"), None),
        (None, _response({"message": "bad gateway"}, status_code=502)),
    ):
        with patch.object(
            agnes_video.requests,
            "post",
            side_effect=side_effect,
            return_value=response,
        ) as post:
            with pytest.raises(agnes_video.AgnesVideoUnconfirmedTaskError):
                agnes_video.generate_videos("sunrise", 5)
        assert post.call_count == 1


def test_submission_redirect_is_unconfirmed_and_never_followed():
    redirect = _response(
        {},
        status_code=307,
        headers={"Location": "https://attacker.example/submit?key=agnes-test-key"},
    )
    with (
        patch.object(agnes_video.requests, "post", return_value=redirect) as post,
        patch.object(agnes_video.requests, "get") as get,
    ):
        with pytest.raises(agnes_video.AgnesVideoUnconfirmedTaskError) as raised:
            agnes_video.generate_videos("sunrise", 5)

    assert "agnes-test-key" not in str(raised.value)
    assert post.call_count == 1
    assert post.call_args.kwargs["allow_redirects"] is False
    get.assert_not_called()


def test_poll_retries_transient_errors_on_same_task():
    submit = _response({"video_id": "agnes-retry"}, 200)
    completed = _response(
        {
            "video_id": "agnes-retry",
            "status": "completed",
            "url": "https://cdn.example.com/retry.mp4",
        }
    )
    with (
        patch.object(agnes_video.requests, "post", return_value=submit) as post,
        patch.object(
            agnes_video.requests,
            "get",
            side_effect=[
                _response({}, status_code=429),
                requests.exceptions.ConnectionError("offline"),
                completed,
            ],
        ) as get,
        patch.object(agnes_video.time, "sleep") as sleep,
    ):
        result = agnes_video.generate_videos("sunrise", 5)

    assert len(result) == 1
    assert post.call_count == 1
    assert get.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


@pytest.mark.parametrize("payload", [{}, {"video_id": "   "}, [], None])
def test_missing_video_id_is_unconfirmed_and_never_polled(payload):
    with (
        patch.object(agnes_video.requests, "post", return_value=_response(payload, 200)),
        patch.object(agnes_video.requests, "get") as get,
    ):
        with pytest.raises(agnes_video.AgnesVideoUnconfirmedTaskError):
            agnes_video.generate_videos("sunrise", 5)
    get.assert_not_called()


def test_completed_task_without_video_url_preserves_task_id():
    with (
        patch.object(
            agnes_video.requests,
            "post",
            return_value=_response({"video_id": "agnes-no-url"}, 200),
        ),
        patch.object(
            agnes_video.requests,
            "get",
            return_value=_response(
                {
                    "video_id": "agnes-no-url",
                    "status": "completed",
                }
            ),
        ),
    ):
        with pytest.raises(agnes_video.AgnesVideoError) as raised:
            agnes_video.generate_videos("sunrise", 5)
    assert raised.value.task_id == "agnes-no-url"


@pytest.mark.parametrize("status", sorted(agnes_video.TERMINAL_FAILURE_STATUSES))
def test_terminal_failure_returns_empty_result(status):
    with patch.object(
        agnes_video.requests,
        "get",
        return_value=_response(
            {
                "video_id": "agnes-failed",
                "status": status,
                "error": {"message": "generation failed"},
            }
        ),
    ):
        result = agnes_video._wait_for_task(
            video_id="agnes-failed",
            api_key="agnes-test-key",
        )
    assert result is None


def test_on_demand_generation_stops_after_required_duration():
    generated_item = MaterialInfo(
        provider="agnes_video",
        url="https://cdn.example.com/video.mp4",
        duration=5,
        source_info={"asset_id": "agnes-paid-1"},
    )
    with (
        patch.object(agnes_video, "generate_videos", return_value=[generated_item]) as generate,
        patch.object(material, "_save_generated_video_with_retry", return_value="/tmp/video.mp4"),
        patch.object(material, "_get_downloaded_video_duration", return_value=5.0),
        patch.object(material, "_persist_material_sources"),
    ):
        paths = material._download_videos_agnes_video_on_demand(
            task_id="local-task",
            search_terms=["first", "second"],
            video_aspect=VideoAspect.landscape,
            audio_duration=5,
            max_clip_duration=5,
            material_directory="/tmp",
        )

    assert paths == ["/tmp/video.mp4"]
    assert generate.call_count == 1
    assert generate.call_args.kwargs["search_term"] == "first"


def test_on_demand_counts_actual_downloaded_duration_before_next_paid_submission():
    generated_items = [
        MaterialInfo(
            provider="agnes_video",
            url="https://cdn.example.com/short.mp4",
            duration=5,
            source_info={"asset_id": "agnes-paid-short"},
        ),
        MaterialInfo(
            provider="agnes_video",
            url="https://cdn.example.com/full.mp4",
            duration=5,
            source_info={"asset_id": "agnes-paid-full"},
        ),
    ]
    with (
        patch.object(
            agnes_video,
            "generate_videos",
            side_effect=[[generated_items[0]], [generated_items[1]]],
        ) as generate,
        patch.object(
            material,
            "_save_generated_video_with_retry",
            side_effect=["/tmp/short.mp4", "/tmp/full.mp4"],
        ),
        patch.object(
            material,
            "_get_downloaded_video_duration",
            side_effect=[2.0, 5.0],
        ),
        patch.object(material, "_persist_material_sources"),
    ):
        paths = material._download_videos_agnes_video_on_demand(
            task_id="local-task",
            search_terms=["short scene", "full scene"],
            video_aspect=VideoAspect.landscape,
            audio_duration=5,
            max_clip_duration=5,
            material_directory="/tmp",
        )

    assert paths == ["/tmp/short.mp4", "/tmp/full.mp4"]
    assert generate.call_count == 2
    assert [call.kwargs["search_term"] for call in generate.call_args_list] == [
        "short scene",
        "full scene",
    ]


@pytest.mark.parametrize("audio_duration", [float("nan"), float("inf"), None, "bad"])
def test_on_demand_rejects_invalid_audio_duration_before_submission(audio_duration):
    with patch.object(agnes_video, "generate_videos") as generate:
        with pytest.raises(agnes_video.AgnesVideoError, match="audio duration"):
            material.download_videos(
                task_id="agnes-invalid-audio",
                search_terms=["one", "two"],
                source="agnes_video",
                audio_duration=audio_duration,
                max_clip_duration=5,
            )
    generate.assert_not_called()


def test_download_failure_stops_later_paid_submissions_and_keeps_task_id():
    generated_item = MaterialInfo(
        provider="agnes_video",
        url="https://cdn.example.com/video.mp4",
        duration=5,
        source_info={"asset_id": "agnes-paid-download"},
    )
    with (
        patch.object(agnes_video, "generate_videos", return_value=[generated_item]) as generate,
        patch.object(material, "_save_generated_video_with_retry", return_value=""),
    ):
        with pytest.raises(agnes_video.AgnesVideoDownloadError) as raised:
            material.download_videos(
                task_id="agnes-download-failed",
                search_terms=["one", "two"],
                source="agnes_video",
                audio_duration=10,
                max_clip_duration=5,
            )
    assert raised.value.task_id == "agnes-paid-download"
    assert generate.call_count == 1


def test_task_preflight_rejects_missing_key_before_script_generation():
    params = VideoParams(video_subject="Agnes preflight", video_source="agnes_video")
    memory_state = sm.MemoryState()
    with (
        patch.object(agnes_video, "is_enabled", return_value=False),
        patch.object(task_service.sm, "state", memory_state),
        patch.object(task_service, "generate_script") as generate_script,
    ):
        result = task_service.start("agnes-preflight", params, stop_at="materials")

    assert result["failed_stage"] == "preflight"
    assert "Agnes video generation requires an Agnes API key" in result["error"]
    generate_script.assert_not_called()


def test_material_failure_keeps_agnes_remote_task_id():
    params = VideoParams(video_subject="space fleet", video_source="agnes_video")
    failure = agnes_video.AgnesVideoUnconfirmedTaskError(
        "remote state unknown", task_id="agnes-recover-1"
    )
    with (
        patch.object(task_service.material, "download_videos", side_effect=failure),
        patch.object(sm.state, "update_task") as update_task,
    ):
        result = task_service.get_video_materials(
            "local-task",
            params,
            ["space fleet"],
            audio_duration=5,
        )

    assert result is None
    failed_update = update_task.call_args.kwargs
    assert failed_update["agnes_task_id"] == "agnes-recover-1"
