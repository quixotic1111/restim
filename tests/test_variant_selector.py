"""The variant selector defaults to the main scripts beside the media.

It used to select the first letter as soon as a variants folder appeared,
which swapped what played for whatever variant A held. Now 'Main' comes
first and a variant plays only once picked.
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QComboBox, QWidget  # noqa: E402

from qt_ui.media_settings_widget import (  # noqa: E402
    MAIN_VARIANT_LABEL, MediaSettingsWidget, variant_selector_index)

_app = QApplication.instance() or QApplication([])


class _Selector:
    """Just the state MediaSettingsWidget's variant methods touch."""
    _update_variant_selector = MediaSettingsWidget._update_variant_selector
    on_variant_changed = MediaSettingsWidget.on_variant_changed
    select_variant_by_letter = MediaSettingsWidget.select_variant_by_letter

    def __init__(self):
        self.available_variants = []
        self.active_variant = None
        self.variant_widget = QWidget()
        self.variant_combobox = QComboBox()
        self.variant_combobox.currentIndexChanged.connect(self.on_variant_changed)
        self.loaded_media_path = 'clip.mp4'
        self.reloads = 0

    def detect_resources_for_media_file(self, path):
        self.reloads += 1

    class _Signal:
        def emit(self):
            pass

    variantSwapStarted = variantSwapped = dialogOpened = _Signal()


VARIANTS = [('A', '/v/A'), ('B', '/v/B')]


def test_index_rule():
    assert variant_selector_index(['A', 'B'], None) == 0
    assert variant_selector_index(['A', 'B'], 'B') == 2
    assert variant_selector_index(['A', 'B'], 'C') == 0


def test_variants_appearing_do_not_select_one():
    s = _Selector()
    s._update_variant_selector(VARIANTS)

    assert s.active_variant is None
    assert s.variant_combobox.currentText() == MAIN_VARIANT_LABEL
    assert [s.variant_combobox.itemText(i) for i in range(3)] == ['Main', 'A', 'B']
    assert s.reloads == 0


def test_picking_a_letter_then_main(tmp_path):
    s = _Selector()
    s._update_variant_selector(VARIANTS)

    assert s.select_variant_by_letter('B')
    assert s.active_variant == 'B' and s.reloads == 1

    assert s.select_variant_by_letter(None)
    assert s.active_variant is None and s.reloads == 2


def test_chosen_variant_survives_a_rescan_and_a_missing_one_falls_back():
    s = _Selector()
    s._update_variant_selector(VARIANTS)
    s.select_variant_by_letter('B')

    s._update_variant_selector(VARIANTS)
    assert s.active_variant == 'B' and s.variant_combobox.currentText() == 'B'

    s._update_variant_selector([('A', '/v/A')])
    assert s.active_variant is None and s.variant_combobox.currentText() == 'Main'


def test_unknown_letter_and_no_variants_are_refused():
    s = _Selector()
    assert not s.select_variant_by_letter('A')
    assert not s.select_variant_by_letter(None)
    s._update_variant_selector(VARIANTS)
    assert not s.select_variant_by_letter('D')
    assert s.active_variant is None
