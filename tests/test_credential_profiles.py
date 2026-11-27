"""Validation of the gopass entry names that credential profiles point at."""

import credential_profiles
import pytest


@pytest.mark.parametrize(
    "entry",
    ["galaxy/main", "accounts/test.galaxyproject.org/user+ci@example.org", "_a/b-c/d.e"],
)
def test_a_profile_accepts_gopass_entry_names_with_email_addresses(tmp_path, entry):
    saved = credential_profiles.update(tmp_path, {"name": "Main", "entry": entry})
    assert [profile["entry"] for profile in saved.values()] == [entry]
    assert credential_profiles.profiles(tmp_path) == saved


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ("galaxy/main key", "cannot contain ' '"),
        ("galaxy/ma$in", "cannot contain “$”"),
        ("galaxy/\nmain", "cannot contain '\\n'"),
        ("../main", "must start with a letter, digit or _"),
        ("galaxy//main", "must start with a letter, digit or _"),
        ("-main", "must start with a letter, digit or _"),
        ("", "up to 255 characters"),
        ("a" * 256, "up to 255 characters"),
    ],
)
def test_a_profile_names_what_is_wrong_with_a_gopass_entry(tmp_path, entry, error):
    with pytest.raises(ValueError) as raised:
        credential_profiles.update(tmp_path, {"name": "Main", "entry": entry})
    assert error in str(raised.value)
    assert credential_profiles.profiles(tmp_path) == {}
