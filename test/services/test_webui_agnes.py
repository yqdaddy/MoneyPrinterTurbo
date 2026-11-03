from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _widget_by_key(elements, key):
    return next(
        item
        for item in elements
        if str(getattr(item, "key", "")) == key
        or str(getattr(item, "key", "")).startswith(f"{key}_")
    )


def _run_generation_flow(source_name, confirm_key, secret_name):
    test_config = dict(
        config.app,
        llm_provider="openai",
        video_source="pexels",
    )
    test_config[secret_name] = "agnes-secret"
    with (
        patch.object(config, "app", test_config),
        patch.object(config, "ui", dict(config.ui, voice_mode="none")),
        patch.object(config, "try_save_config", return_value=True),
        patch("app.services.webui_task.submit_generation") as submit_generation,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()

        _widget_by_key(app.text_area, "video_subject").set_value("AI office").run()
        _widget_by_key(app.text_area, "video_script").set_value(
            "AI helps people work faster."
        ).run()
        _widget_by_key(app.text_area, "video_terms").set_value(
            "office worker, AI assistant"
        ).run()
        app.session_state["video_source_select_en"] = source_name
        app.run()

        _widget_by_key(app.button, "generate_video_button").click().run()
        assert submit_generation.call_count == 0
        assert any("confirm" in str(item.value).lower() for item in app.error)

        _widget_by_key(app.checkbox, confirm_key).check().run()
        _widget_by_key(app.button, "generate_video_button").click().run()

        assert submit_generation.call_count == 1
        submitted_params = submit_generation.call_args.kwargs["params"]
        assert submitted_params.video_source == source_name
        assert "agnes-secret" not in submitted_params.model_dump_json()
        assert [str(item.value) for item in app.exception] == []


def test_agnes_video_source_requires_confirmation_then_submits_without_secret():
    _run_generation_flow("agnes_video", "agnes_video_confirm_charge",
                         "agnes_video_api_key")


def test_agnes_image_source_requires_confirmation_then_submits_without_secret():
    _run_generation_flow("agnes_image", "agnes_image_confirm_charge",
                         "agnes_image_api_keys")
