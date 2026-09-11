from validate_skill import validate_skill


def test_repository_skill_is_valid():
    from pathlib import Path

    assert validate_skill(Path(__file__).resolve().parent.parent) == []


def test_missing_and_malformed_skill_are_rejected(tmp_path):
    assert validate_skill(tmp_path)
    (tmp_path / "SKILL.md").write_text("---\n[not a mapping]\n---\n")
    assert validate_skill(tmp_path)
    (tmp_path / "SKILL.md").write_text("---\nname: [\n---\n")
    assert validate_skill(tmp_path)


def test_empty_description_and_broken_references_are_rejected(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        '---\nname: example\ndescription: ""\n---\n[Missing](references/missing.md)'
    )
    errors = validate_skill(tmp_path)
    assert len(errors) == 2


def test_valid_local_and_external_links(tmp_path):
    (tmp_path / "notes.md").write_text("Notes")
    (tmp_path / "SKILL.md").write_text(
        "---\nname: example\ndescription: Valid skill\n---\n[Notes](notes.md#section) [External](https://example.com)"
    )
    assert validate_skill(tmp_path) == []
