"""The settings the page keeps with the watcher: task prompts and the main reviewer."""

import json

import dashboard_settings as settings
import pytest


def test_a_home_without_a_file_has_no_prompts_and_a_main_reviewer(tmp_path):
    assert settings.settings(tmp_path) == {"task_prompts": {}, "reviewer": "codex"}


def test_a_saved_prompt_replaces_the_pages_own_until_it_is_cleared(tmp_path):
    saved = settings.save(tmp_path, {"task": "prReview", "prompt": "Read {url} closely."})
    assert saved["task_prompts"] == {"prReview": "Read {url} closely."}
    assert settings.settings(tmp_path) == saved
    # Another task is stored beside it, not instead of it.
    both = settings.save(tmp_path, {"task": "ci", "prompt": "Fix {url}."})
    assert both["task_prompts"] == {"prReview": "Read {url} closely.", "ci": "Fix {url}."}
    cleared = settings.save(tmp_path, {"task": "prReview", "prompt": "  "})
    assert cleared["task_prompts"] == {"ci": "Fix {url}."}


def test_the_main_reviewer_is_saved_on_its_own(tmp_path):
    settings.save(tmp_path, {"task": "issue", "prompt": "Resolve {url}."})
    saved = settings.save(tmp_path, {"reviewer": "claude"})
    assert saved == {"task_prompts": {"issue": "Resolve {url}."}, "reviewer": "claude"}
    assert settings.save(tmp_path, {"reviewer": "codex"})["reviewer"] == "codex"


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ({"task": "prReview", "prompt": "Review the pull request."}, "{url}"),
        ({"task": "prReview", "prompt": "{url}" + "x" * settings.LIMIT}, "under 8000"),
        ({"task": "prReview", "prompt": 7}, "as text"),
        ({"task": "unknown", "prompt": "{url}"}, "Unknown task"),
        ({"reviewer": "gemini"}, "Codex or Claude"),
        ({"task": "ci", "prompt": "{url}", "reviewer": "claude"}, "either"),
        ({}, "either"),
        ({"prompt": "{url}"}, "either"),
        ({"task": "ci", "prompt": "{url}", "agent": "codex"}, "Unknown setting"),
        ("reviewer", "Unknown setting"),
    ],
)
def test_a_setting_that_would_not_work_is_refused(tmp_path, request_, message):
    with pytest.raises(ValueError, match=message):
        settings.save(tmp_path, request_)
    assert not (tmp_path / settings.SETTINGS).exists()


def test_a_hand_edited_file_keeps_only_what_the_page_can_use(tmp_path):
    (tmp_path / settings.SETTINGS).write_text(
        json.dumps(
            {
                "task_prompts": {"prReview": "Review {url}.", "deploy": "Ship it", "ci": "   "},
                "reviewer": "gemini",
                "theme": "dark",
            }
        )
    )
    assert settings.settings(tmp_path) == {
        "task_prompts": {"prReview": "Review {url}."},
        "reviewer": "codex",
    }
    # Saving beside it writes back only the understood settings.
    settings.save(tmp_path, {"reviewer": "claude"})
    assert json.loads((tmp_path / settings.SETTINGS).read_text()) == {
        "task_prompts": {"prReview": "Review {url}."},
        "reviewer": "claude",
    }


def test_an_unreadable_file_leaves_the_page_on_its_own_wording(tmp_path):
    (tmp_path / settings.SETTINGS).write_text("{ not json")
    assert settings.settings(tmp_path) == {"task_prompts": {}, "reviewer": "codex"}
    (tmp_path / settings.SETTINGS).write_text(json.dumps(["prReview"]))
    assert settings.settings(tmp_path) == {"task_prompts": {}, "reviewer": "codex"}
