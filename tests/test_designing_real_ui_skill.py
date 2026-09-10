from pathlib import Path

from cptr.utils.skills import discover_skills, load_skill, parse_frontmatter


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".cptr" / "skills" / "designing-real-ui"
SKILL_MD = SKILL_DIR / "SKILL.md"
CONTRACT_REF = SKILL_DIR / "references" / "ui-feature-contract.md"


def test_designing_real_ui_skill_is_discoverable_and_enforces_real_functionality():
    assert SKILL_MD.is_file(), "designing-real-ui SKILL.md must exist"
    assert CONTRACT_REF.is_file(), "UI feature-contract reference must exist"

    content = SKILL_MD.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)

    assert frontmatter["name"] == "designing-real-ui"
    assert frontmatter["version"] == "0.1.0"
    assert frontmatter["description"] == "Use when designing or redesigning a working product UI."

    required_skill_phrases = [
        "feature-contract ledger",
        "real backend",
        "existing theme",
        "live state",
        "prefers-reduced-motion",
        "managed browser",
        "console",
        "network",
        "fail closed",
    ]
    lowered_body = body.lower()
    for phrase in required_skill_phrases:
        assert phrase.lower() in lowered_body, f"missing UI contract clause: {phrase}"

    reference = CONTRACT_REF.read_text(encoding="utf-8").lower()
    for field in [
        "ui element",
        "backend authority",
        "request/input",
        "response/state",
        "live transport",
        "authorization",
        "verification evidence",
        "disposition",
    ]:
        assert field in reference, f"feature-contract ledger missing field: {field}"

    discovered = {skill.name: skill for skill in discover_skills(str(ROOT))}
    assert "designing-real-ui" in discovered
    assert discovered["designing-real-ui"].source == "workspace"

    loaded = load_skill(str(ROOT), "designing-real-ui")
    assert loaded is not None
    assert "references/ui-feature-contract.md" in loaded.resources
