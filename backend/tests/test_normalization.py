from app.normalization import dataset_content_hash, normalize_text, sha256_text


def test_normalization_preserves_meaningful_whitespace() -> None:
    assert normalize_text("a\r\nb") == "a\nb"
    assert sha256_text("é") == sha256_text("e\u0301")
    assert sha256_text("a ") != sha256_text("a")


def test_dataset_hash_does_not_depend_on_row_order() -> None:
    rows = [
        {"sample_id": "2", "source_language": "de", "source_text": "B", "reference_zh": "乙"},
        {"sample_id": "1", "source_language": "de", "source_text": "A", "reference_zh": "甲"},
    ]
    assert dataset_content_hash(rows) == dataset_content_hash(list(reversed(rows)))

