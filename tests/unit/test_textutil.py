import pytest

from localasistan.textutil import fold_text


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ÇALIŞMA", "çalışma"),
        ("İstanbul", "istanbul"),
        ("ISPARTA", "ısparta"),
        ("INFO", "info"),
        ("Şükrü", "ŞÜKRÜ"),
        ("i̇stanbul", "İstanbul"),  # NFD biçimli İ
    ],
)
def test_fold_equal(a: str, b: str):
    assert fold_text(a) == fold_text(b)


def test_fold_distinguishes_other_letters():
    assert fold_text("göz") != fold_text("goz")
    assert fold_text("a") != fold_text("b")
