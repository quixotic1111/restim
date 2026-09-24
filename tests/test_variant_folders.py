"""Variant folders: A..D alternatives for one media file.

They live in `<scene>/variants/`, or in the older sibling layout
`<scene>_variants/`; the selector must find either.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from funscript.collect_funscripts import detect_variant_folders  # noqa: E402


def _letters(variants):
    return [letter for letter, _ in variants]


def test_variants_inside_the_media_folder(tmp_path):
    for letter in 'BA':
        (tmp_path / 'clip' / 'variants' / letter).mkdir(parents=True)

    variants = detect_variant_folders(str(tmp_path / 'clip.mp4'))

    assert _letters(variants) == ['A', 'B']
    assert variants[0][1] == str(tmp_path / 'clip' / 'variants' / 'A')


def test_older_sibling_variants_folder(tmp_path):
    (tmp_path / 'clip_variants' / 'A').mkdir(parents=True)

    variants = detect_variant_folders(str(tmp_path / 'clip.mp4'))

    assert variants == [('A', str(tmp_path / 'clip_variants' / 'A'))]


def test_new_location_wins_over_the_old(tmp_path):
    (tmp_path / 'clip' / 'variants' / 'A').mkdir(parents=True)
    (tmp_path / 'clip_variants' / 'B').mkdir(parents=True)

    assert _letters(detect_variant_folders(str(tmp_path / 'clip.mp4'))) == ['A']


def test_only_single_capital_letters_are_variants(tmp_path):
    for name in ('A', 'b', 'AB', 'notes'):
        (tmp_path / 'clip' / 'variants' / name).mkdir(parents=True)
    (tmp_path / 'clip' / 'variants' / 'C').write_text('a file, not a folder')

    assert _letters(detect_variant_folders(str(tmp_path / 'clip.mp4'))) == ['A']


def test_no_variants_folder(tmp_path):
    (tmp_path / 'clip').mkdir()

    assert detect_variant_folders(str(tmp_path / 'clip.mp4')) == []
    assert detect_variant_folders('') == []
