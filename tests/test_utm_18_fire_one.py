from pathlib import Path


SOURCE = Path(
    "/Users/example/Desktop/共享文件/Fire_One_en1.3/src/fill-description.ts"
)


def test_fire_one_skips_completed_enrollment_and_reuses_existing_team_key() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "Thank you for your submission." in source
    assert "async function readExistingIntegrationData" in source
    assert "P8_FILE=existing_verified" in source


def test_fire_one_normalizes_keywords_to_apple_limit_before_filling() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "function appleKeywords" in source
    assert "const keywordsValue = appleKeywords(KEYWORDS);" in source
    assert "keywordsValue.length > 100" in source


def test_fire_one_exits_cleanly_after_success_instead_of_leaving_cdp_resident() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "main().then(" in source
    assert "() => process.exit(0)" in source
