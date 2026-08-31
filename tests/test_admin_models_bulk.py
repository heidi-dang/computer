from cptr.utils.model_config import apply_bulk_model_active_state


def test_bulk_model_active_state_preserves_existing_model_config():
    original = {
        "gemini-2.5-pro": {
            "is_active": True,
            "params": {"system_prompt": "keep me", "request_params": {"temperature": 0.2}},
        },
        "gemini-2.5-flash": {"params": {"builtin_tools": {"web": False}}},
        "claude-sonnet": {"is_active": False, "params": {"system_prompt": "untouched"}},
    }

    updated = apply_bulk_model_active_state(
        original,
        ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-pro"],
        False,
    )

    assert updated is not original
    assert updated["gemini-2.5-pro"] == {
        "is_active": False,
        "params": {"system_prompt": "keep me", "request_params": {"temperature": 0.2}},
    }
    assert updated["gemini-2.5-flash"] == {
        "is_active": False,
        "params": {"builtin_tools": {"web": False}},
    }
    assert updated["claude-sonnet"] == {
        "is_active": False,
        "params": {"system_prompt": "untouched"},
    }
    assert original["gemini-2.5-pro"]["is_active"] is True


def test_bulk_enable_cleans_redundant_default_active_entry():
    updated = apply_bulk_model_active_state(
        {
            "gemini-2.5-flash": {"is_active": False},
            "gemini-2.5-pro": {"is_active": False, "params": {"system_prompt": "keep"}},
        },
        ["gemini-2.5-flash", "gemini-2.5-pro"],
        True,
    )

    assert "gemini-2.5-flash" not in updated
    assert updated["gemini-2.5-pro"] == {"is_active": True, "params": {"system_prompt": "keep"}}
