from okb.normalize import MAX_CHUNK_CHARS, has_meaningful_text, split_chunks


def test_meaningful_text_counts_letters_only():
    assert has_meaningful_text("Договор поставки товара")
    assert not has_meaningful_text("12345 !!! ---")


def test_ocr_noise_is_not_meaningful():
    assert not has_meaningful_text("<!-- page 1 -->\n(empty page)\n<!-- page 2 -->")


def test_split_short_doc_is_one_chunk():
    md = "# Заголовок\n\n" + "Осмысленный текст договора. " * 20
    chunks = split_chunks(md)
    assert len(chunks) == 1


def test_split_respects_max_size():
    md = "\n\n".join(f"Пункт {i}. " + "слово " * 300 for i in range(10))
    chunks = split_chunks(md)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= MAX_CHUNK_CHARS + 100  # +joining slack


def test_split_keeps_all_content():
    md = "\n\n".join(f"UNIQUE_MARKER_{i} " + "текст " * 200 for i in range(8))
    joined = "\n".join(split_chunks(md))
    for i in range(8):
        assert f"UNIQUE_MARKER_{i}" in joined


def test_split_drops_sub_min_chunks():
    assert split_chunks("тс", min_chars=200) == []
