"""Funscript discovery: a media file's scripts can live in more than one place.

Beside the video, in a directory named after it, and in a zipfile named after
it. The search used to stop at the first directory that yielded any script, so
with the main script beside the video the named directory and zipfile were
never read, and a backup zipfile was searched like any other.
"""
import sys
import pathlib
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from funscript.collect_funscripts import collect_funscripts  # noqa: E402


def _touch(path: pathlib.Path, text='{"actions": []}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _zip(path: pathlib.Path, members: dict[str, str]):
    with zipfile.ZipFile(path, 'w') as z:
        for name, text in members.items():
            z.writestr(name, text)


def _found(resources):
    """(type, containing folder or zip name) for each collected script."""
    out = []
    for r in resources:
        name = r.name()
        parts = name.split('.')
        kind = parts[-2] if len(parts) >= 3 else ''
        out.append((kind, pathlib.PurePath(str(r)).parent.name))
    return sorted(out)


def test_loose_scripts_only(tmp_path):
    _touch(tmp_path / 'clip.funscript')
    _touch(tmp_path / 'clip.alpha.funscript')
    _touch(tmp_path / 'other.funscript')

    found = _found(collect_funscripts([str(tmp_path)], 'clip.mp4'))

    assert found == [('', tmp_path.name), ('alpha', tmp_path.name)]


def test_named_directory_is_read_beside_loose_scripts(tmp_path):
    _touch(tmp_path / 'clip.funscript')
    _touch(tmp_path / 'clip' / 'clip.alpha.funscript')
    _touch(tmp_path / 'clip' / 'clip.beta.funscript')

    found = _found(collect_funscripts([str(tmp_path)], 'clip.mp4'))

    assert found == [('', tmp_path.name), ('alpha', 'clip'), ('beta', 'clip')]


def test_named_directory_alone(tmp_path):
    _touch(tmp_path / 'clip' / 'clip.funscript')
    _touch(tmp_path / 'clip' / 'clip.alpha.funscript')

    found = _found(collect_funscripts([str(tmp_path)], 'clip.mp4'))

    assert found == [('', 'clip'), ('alpha', 'clip')]


def test_nearest_copy_of_a_type_wins(tmp_path):
    _touch(tmp_path / 'clip.alpha.funscript', 'loose')
    _touch(tmp_path / 'clip' / 'clip.alpha.funscript', 'folder')
    _zip(tmp_path / 'clip.zip', {'clip.alpha.funscript': 'zip',
                                 'clip.beta.funscript': 'zip'})

    resources = collect_funscripts([str(tmp_path)], 'clip.mp4')

    by_type = {}
    for r in resources:
        kind = r.funscript_type()
        assert kind not in by_type, f'{kind} collected twice'
        with r.open() as f:
            by_type[kind] = f.read()
    assert by_type == {'alpha': 'loose', 'beta': 'zip'}


def test_named_directory_outranks_named_zip(tmp_path):
    _touch(tmp_path / 'clip' / 'clip.alpha.funscript', 'folder')
    _zip(tmp_path / 'clip.zip', {'clip.alpha.funscript': 'zip'})

    resources = collect_funscripts([str(tmp_path)], 'clip.mp4')

    assert len(resources) == 1
    with resources[0].open() as f:
        assert f.read() == 'folder'


def test_named_zip_is_read_beside_loose_scripts(tmp_path):
    _touch(tmp_path / 'clip.funscript')
    _zip(tmp_path / 'clip.zip', {'clip.alpha.funscript': '{}'})

    found = _found(collect_funscripts([str(tmp_path)], 'clip.mp4'))

    assert found == [('', tmp_path.name), ('alpha', 'clip.zip')]


def test_backup_zip_is_skipped(tmp_path):
    _touch(tmp_path / 'clip.funscript')
    _zip(tmp_path / 'clip.backup-20260913_101500.zip',
         {'clip.alpha.funscript': '{}'})

    found = _found(collect_funscripts([str(tmp_path)], 'clip.mp4'))

    assert found == [('', tmp_path.name)]


def test_backup_zip_alone_yields_nothing(tmp_path):
    _zip(tmp_path / 'clip.backup-20260913_101500.zip',
         {'clip.funscript': '{}'})

    assert collect_funscripts([str(tmp_path)], 'clip.mp4') == []


def test_unnamed_subdirectories_are_not_read(tmp_path):
    # variants/, versions/ and backups/ inside the named directory hold
    # alternatives and history, not the scripts for this media.
    _touch(tmp_path / 'clip' / 'clip.funscript')
    _touch(tmp_path / 'clip' / 'variants' / 'A' / 'clip.alpha.funscript')
    _touch(tmp_path / 'clip' / 'versions' / '20260913' / 'clip.beta.funscript')

    found = _found(collect_funscripts([str(tmp_path)], 'clip.mp4'))

    assert found == [('', 'clip')]


def test_later_search_paths_stay_a_fallback(tmp_path):
    home = tmp_path / 'videos'
    library = tmp_path / 'library'
    _touch(home / 'clip.funscript')
    _touch(library / 'clip.alpha.funscript')

    found = _found(collect_funscripts([str(home), str(library)], 'clip.mp4'))

    assert found == [('', 'videos')]


def test_later_search_path_used_when_home_is_empty(tmp_path):
    home = tmp_path / 'videos'
    library = tmp_path / 'library'
    home.mkdir()
    _touch(library / 'clip.alpha.funscript')
    _touch(library / 'clip' / 'clip.beta.funscript')

    found = _found(collect_funscripts([str(home), str(library)], 'clip.mp4'))

    assert found == [('alpha', 'library'), ('beta', 'clip')]


def test_recursive_search_stops_at_the_first_subdirectory_with_scripts(tmp_path):
    # A '<dir>/*' search path walks unrelated subdirectories until one yields
    # scripts; it must not gather same-named scripts from all of them.
    _touch(tmp_path / 'a' / 'clip.alpha.funscript')
    _touch(tmp_path / 'b' / 'clip.beta.funscript')

    found = collect_funscripts([str(tmp_path) + '/*'], 'clip.mp4')

    assert len(found) == 1


def test_type_match_is_case_insensitive(tmp_path):
    _touch(tmp_path / 'clip.Alpha.funscript', 'loose')
    _touch(tmp_path / 'clip' / 'clip.alpha.funscript', 'folder')

    resources = collect_funscripts([str(tmp_path)], 'clip.mp4')

    assert len(resources) == 1
    with resources[0].open() as f:
        assert f.read() == 'loose'
