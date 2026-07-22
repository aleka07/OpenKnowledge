"""Name folding for cross-script search.

One person appears as Baurzhan / Bauyrzhan / Бауыржан / Амирханов Б.С. —
four different strings for Postgres. All person names are folded to lowercase
latin at ingest (meta.people_norm) and matched fuzzily via pg_trgm, so a
one-letter transliteration difference still matches.
"""

# Russian + Kazakh Cyrillic -> latin, common romanization
_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Kazakh-specific
    "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u",
    "һ": "h", "і": "i",
}


def translit(text: str) -> str:
    return "".join(_MAP.get(ch, ch) for ch in text.lower())


def fold_names(names: list[str]) -> str:
    """Space-joined lowercase-latin folding of all names, for meta.people_norm."""
    return " ".join(dict.fromkeys(translit(n).strip() for n in names if n and n.strip()))
