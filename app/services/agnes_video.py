"""Agnes asynchronous text-to-video client.

The provider-specific submit/poll protocol lives here so the material service
only needs to deal with ``MaterialInfo`` and local downloads.  Agnes jobs are
billable once accepted, so submission is deliberately never retried when the
response is ambiguous.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote_plus, urlsplit

import requests
from loguru import logger

from app.utils.request_logging import trace_http_call

from app.config import config
from app.services import generation_progress
from app.models.schema import MaterialInfo, VideoAspect


DEFAULT_BASE_URL = "https://api.agnes-ai.cn/v1"
# Agnes Video 2.5 Flash supports 4-12 second clips and fixed 720P output.
DEFAULT_MODEL = "agnes-video-2.5-flash"
DEFAULT_SIZE = "720P"
DEFAULT_MIN_DURATION_SECONDS = 4
DEFAULT_MAX_DURATION_SECONDS = 12
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
MAX_POLL_RETRIES = 5
RETRY_BASE_SECONDS = 1.0
MAX_ERROR_TEXT_LENGTH = 500
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
ACTIVE_STATUSES = frozenset({"queued", "pending", "processing", "running"})
TERMINAL_SUCCESS_STATUSES = frozenset({"completed"})
TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled", "canceled", "expired"})


class AgnesVideoError(RuntimeError):
    """Deterministic configuration, request, or response error."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        self.task_id = task_id


class AgnesVideoUnconfirmedTaskError(AgnesVideoError):
    """The remote job may exist, but its final state cannot be confirmed."""


class AgnesVideoDownloadError(AgnesVideoError):
    """A billable job completed, but its output could not be downloaded."""


def get_api_key(settings: Mapping[str, Any] | None = None) -> str:
    """Read the dedicated Agnes video key, then the provider-specific env key."""
    settings = config.app if settings is None else settings
    configured = str(settings.get("agnes_video_api_key", "") or "").strip()
    environment_key = os.getenv("AGNES_VIDEO_API_KEY", "").strip()
    return configured or environment_key


def is_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    return bool(get_api_key(settings))


def _base_url() -> str:
    value = (
        str(
            config.app.get("agnes_video_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        )
        .strip()
        .rstrip("/")
    )
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AgnesVideoError("agnes_video_base_url must be an absolute HTTP(S) URL")
    return value


def _model_name() -> str:
    value = str(
        config.app.get("agnes_video_model_name", DEFAULT_MODEL) or DEFAULT_MODEL
    ).strip()
    return value or DEFAULT_MODEL


def _poll_url(base_url: str, video_id: str, model_name: str) -> str:
    """
    Build the task-status URL from the API host root.

    Agnes serves status queries at ``{scheme}://{host}/agnesapi`` — above the
    ``/v1`` API root — so the path of the configured base URL is deliberately
    discarded.
    """
    parsed = urlsplit(base_url)
    return (
        f"{parsed.scheme}://{parsed.netloc}/agnesapi"
        f"?video_id={quote_plus(video_id)}"
        f"&model_name={quote_plus(model_name)}"
    )


def _config_bool(key: str, default: bool) -> bool:
    value = config.app.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _bounded_float(key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.app.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)


def _duration_bounds() -> tuple[int, int]:
    def read(key: str, default: int) -> int:
        try:
            value = int(config.app.get(key, default))
        except (TypeError, ValueError, OverflowError):
            # TOML accepts `inf`; int(inf) raises OverflowError, which is not
            # a provider error and would abort the task with a traceback.
            return default
        return value if value >= 1 else default

    minimum = read("agnes_video_min_duration", DEFAULT_MIN_DURATION_SECONDS)
    maximum = read("agnes_video_max_duration", DEFAULT_MAX_DURATION_SECONDS)
    return minimum, max(minimum, maximum)


def _tls_verify() -> bool:
    return _config_bool("tls_verify", True)


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _redact_secret(value: Any, secret: str) -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "***")
        encoded = quote_plus(secret)
        if encoded != secret:
            text = text.replace(encoded, "***")
    for proxy_url in config.proxy.values():
        proxy_secret = str(proxy_url or "")
        if proxy_secret:
            text = text.replace(proxy_secret, "***")
    return text[:MAX_ERROR_TEXT_LENGTH]


def _response_error(response: Any, api_key: str) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {_status_code(response)}"
    if not isinstance(payload, dict):
        return f"HTTP {_status_code(response)}"

    error = payload.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or error.get("detail") or error.get("code")
    else:
        detail = payload.get("detail") or payload.get("message") or error
    if isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail)
    return _redact_secret(detail or f"HTTP {_status_code(response)}", api_key)


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    response = getattr(error, "response", None)
    return response is not None and _status_code(response) in RETRYABLE_STATUS_CODES


def generate_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    """Submit one Agnes text-to-video job and wait for its downloadable output."""
    api_key = get_api_key()
    if not api_key:
        raise AgnesVideoError("Agnes video generation requires an Agnes API key")

    term = str(search_term or "").strip()
    if not term:
        raise AgnesVideoError("Agnes video search term must not be empty")

    try:
        aspect = VideoAspect(video_aspect)
    except (TypeError, ValueError) as exc:
        raise AgnesVideoError("Agnes video aspect is invalid") from exc
    try:
        requested_duration = max(int(minimum_duration), 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgnesVideoError("Agnes clip duration must be a positive integer") from exc
    minimum, maximum = _duration_bounds()
    duration = min(max(requested_duration, minimum), maximum)
    if duration != requested_duration:
        logger.info(
            "agnes clip duration clamped to the configured endpoint range: "
            f"requested={requested_duration}s, using={duration}s "
            f"(configured {minimum}-{maximum}s)"
        )

    base_url = _base_url()
    model_name = _model_name()
    videos_url = f"{base_url}/videos"
    # The Agnes API takes the clip length as a string of whole seconds.
    payload = {
        "model": model_name,
        "prompt": term,
        "mode": "text",
        "seconds": str(duration),
        "size": DEFAULT_SIZE,
        "aspect_ratio": aspect.value,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    logger.info(
        "generating video with Agnes: "
        f"model={model_name}, term={term!r}, duration={duration}s"
    )

    # A timeout or 5xx after POST may mean that Agnes accepted and billed the
    # job.  Retrying the submission could create a second paid generation.
    generation_progress.submitting("agnes_video")
    try:
        response = trace_http_call(
            "POST",
            requests.post,
            videos_url,
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_tls_verify(),
            timeout=(30, 60),
            allow_redirects=False,
        )
    except Exception as exc:
        raise AgnesVideoUnconfirmedTaskError(
            "Agnes submission returned no response; a paid task may already "
            "exist remotely: "
            f"error={type(exc).__name__}, detail={_redact_secret(exc, api_key)}"
        ) from exc

    status_code = _status_code(response)
    if 300 <= status_code < 400:
        location = getattr(response, "headers", {}).get("Location", "")
        raise AgnesVideoUnconfirmedTaskError(
            "Agnes submission returned an unexpected redirect; the paid task "
            "state is unknown: "
            f"HTTP {status_code}, location={_redact_secret(location, api_key)}"
        )
    if status_code >= 500:
        raise AgnesVideoUnconfirmedTaskError(
            f"Agnes submission failed with HTTP {status_code}; a paid task may "
            "already exist remotely"
        )
    if not 200 <= status_code < 300:
        raise AgnesVideoError(
            "Agnes video generation request rejected: "
            f"HTTP {status_code}, {_response_error(response, api_key)}"
        )
    try:
        body = response.json()
    except Exception as exc:
        raise AgnesVideoUnconfirmedTaskError(
            "Agnes submission returned an unreadable response; a paid task may "
            f"already exist remotely: error={type(exc).__name__}"
        ) from exc
    # The submission response distinguishes video_id (used for status queries)
    # from the generic id/task_id task identifiers.
    video_id = str(body.get("video_id") or "").strip() if isinstance(body, dict) else ""
    if not video_id:
        raise AgnesVideoUnconfirmedTaskError(
            "Agnes accepted the submission without returning a video id"
        )
    logger.info(f"Agnes video task created: video_id={video_id}")

    task = _wait_for_task(
        video_id=video_id,
        api_key=api_key,
        model_name=model_name,
        base_url=base_url,
    )
    if task is None:
        return []

    video_url = str(task.get("url") or "").strip()
    if not video_url.startswith(("http://", "https://")):
        raise AgnesVideoError(
            f"Agnes task completed without a downloadable video: id={video_id}",
            task_id=video_id,
        )

    video_width, video_height = aspect.to_resolution()
    return [
        MaterialInfo(
            provider="agnes_video",
            url=video_url,
            duration=duration,
            source_info={
                "provider": "agnes_video",
                "search_term": term,
                "asset_id": video_id,
                "rendition": {
                    "id": video_id,
                    "width": video_width,
                    "height": video_height,
                },
            },
        )
    ]


def _wait_for_task(
    *,
    video_id: str,
    api_key: str,
    model_name: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + _bounded_float(
        "agnes_video_run_timeout",
        DEFAULT_RUN_TIMEOUT_SECONDS,
        60.0,
        7200.0,
    )
    poll_interval = _bounded_float(
        "agnes_video_poll_interval",
        DEFAULT_POLL_INTERVAL_SECONDS,
        0.5,
        60.0,
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    result_url = _poll_url(base_url, video_id, model_name)
    generation_progress.remote("agnes_video", video_id)
    consecutive_failures = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgnesVideoUnconfirmedTaskError(
                "Agnes task is still running after the configured local wait "
                f"timeout: id={video_id}",
                task_id=video_id,
            )

        phase_timeout = max(min(remaining / 2.0, 30.0), 0.001)
        try:
            response = trace_http_call(
                "GET",
                requests.get,
                result_url,
                headers=headers,
                proxies=config.proxy,
                verify=_tls_verify(),
                timeout=(phase_timeout, phase_timeout),
                allow_redirects=False,
            )
            status_code = _status_code(response)
            if 300 <= status_code < 400:
                location = getattr(response, "headers", {}).get("Location", "")
                raise AgnesVideoUnconfirmedTaskError(
                    "Agnes polling returned an unexpected redirect; the paid "
                    "task state is unknown: "
                    f"HTTP {status_code}, location={_redact_secret(location, api_key)}",
                    task_id=video_id,
                )
            if status_code in RETRYABLE_STATUS_CODES:
                raise requests.exceptions.HTTPError(
                    f"HTTP {status_code}", response=response
                )
            if not 200 <= status_code < 300:
                raise AgnesVideoUnconfirmedTaskError(
                    "Agnes task status is unknown: "
                    f"http_status={status_code}, "
                    f"detail={_response_error(response, api_key)}",
                    task_id=video_id,
                )
            body = response.json()
            if not isinstance(body, dict):
                raise AgnesVideoUnconfirmedTaskError(
                    "Agnes task status response is malformed", task_id=video_id
                )
        except AgnesVideoUnconfirmedTaskError:
            raise
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise AgnesVideoUnconfirmedTaskError(
                    "Agnes polling failed and the task state is unknown: "
                    f"error={type(exc).__name__}, "
                    f"detail={_redact_secret(exc, api_key)}",
                    task_id=video_id,
                ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgnesVideoUnconfirmedTaskError(
                    "Agnes task is still running after the configured local wait "
                    f"timeout: id={video_id}",
                    task_id=video_id,
                ) from exc
            consecutive_failures += 1
            generation_progress.remote("agnes_video", video_id, retrying=True)
            if consecutive_failures > MAX_POLL_RETRIES:
                raise AgnesVideoUnconfirmedTaskError(
                    "Agnes polling failed after retries; the paid task may still "
                    f"be running remotely: id={video_id}",
                    task_id=video_id,
                ) from exc
            delay = min(RETRY_BASE_SECONDS * consecutive_failures, remaining)
            logger.warning(
                "Agnes polling hit a transient error; retrying the same task: "
                f"id={video_id}, attempt={consecutive_failures}/{MAX_POLL_RETRIES}, "
                f"retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        consecutive_failures = 0
        status = str(body.get("status") or "").strip().lower()
        generation_progress.remote("agnes_video", video_id, status)
        if status in TERMINAL_SUCCESS_STATUSES:
            return body
        if status in TERMINAL_FAILURE_STATUSES:
            logger.error(
                "Agnes task did not produce a video: "
                f"id={video_id}, status={status}, "
                f"detail={_redact_secret(body.get('error'), api_key)}"
            )
            return None
        if status not in ACTIVE_STATUSES:
            raise AgnesVideoUnconfirmedTaskError(
                f"Agnes returned an unknown task status: id={video_id}, "
                f"status={status!r}",
                task_id=video_id,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgnesVideoUnconfirmedTaskError(
                "Agnes task is still running after the configured local wait "
                f"timeout: id={video_id}",
                task_id=video_id,
            )
        time.sleep(min(poll_interval, remaining))
