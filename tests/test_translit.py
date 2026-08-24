from okb.translit import fold_names, translit


def test_russian_name():
    assert translit("Бауыржан") == "bauyrzhan"


def test_kazakh_specific_letters():
    # ә ғ қ ң ө ұ ү і all have latin foldings
    assert translit("Әміржан Құралбекұлы") == "amirzhan kuralbekuly"


def test_latin_passthrough():
    assert translit("Baurzhan S.") == "baurzhan s."


def test_same_person_different_scripts_fold_close():
    # the whole point: cyrillic and latin spellings of one person converge
    assert translit("Амирханова") == "amirkhanova"


def test_fold_names_converges_scripts_and_dedups():
    # cyrillic and latin spellings fold to the SAME string and dedup to one
    folded = fold_names(["Амирханова Г.А.", "Amirkhanova G.A.", "Амирханова Г.А."])
    assert folded == "amirkhanova g.a."


def test_fold_names_skips_empty():
    assert fold_names(["", "  ", "Иванов"]) == "ivanov"
