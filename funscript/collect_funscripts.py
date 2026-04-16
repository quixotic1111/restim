import os
import re
import zipfile
import pathlib
import logging
# from importlib.abc import Traversable # since python 3.11

logger = logging.getLogger('restim.funscript')

VARIANT_FOLDER_PATTERN = re.compile(r'^[A-Z]$')


def case_insensitive_compare(a, b):
    return a.lower() == b.lower()


def split_funscript_path(path):
    a, b = os.path.split(path)
    parts = b.split('.')
    extension = parts[-1]
    if len(parts) == 1:
        return parts[0], '', ''
    if len(parts) == 2:
        return parts[0], '', extension
    return '.'.join(parts[:-2]), parts[-2], extension


class Resource:
    def __init__(self, path):
        self.path = path  # Traversable, since python 3.11

    def open(self, *args, **kwargs):
        return self.path.open(*args, **kwargs)

    def is_funscript(self):
        return case_insensitive_compare(self.path.suffix, '.funscript')

    def funscript_type(self):
        try:
            return self.path.suffixes[-2][1:].lower()
        except IndexError:
            return ''

    def name(self):
        return self.path.name

    def __str__(self):
        return str(self.path)

    def __repr__(self):
        return self.path.__repr__()


def collect_funscripts(
        dirs: list[str],
        media: str
) -> list[Resource]:
    """
    Search the directories in order for funscripts. Stop searching when at least one funscript is found in a directly.
    If a directory is found with the same name as the media, search that directory too.
    zipfiles are supported.
    :param dirs:
    :param media:
    :return:
    """
    def path_is_zip(path):
        try:
            zipfile.Path(path)
            return True
        except OSError:
            return False

    dir_stack = dirs[:]
    new_dirs = []
    collected_files = []

    media_prefix, _, media_extension = split_funscript_path(media)

    while dir_stack and len(collected_files) == 0:
        try:
            current_dir = os.path.expanduser(dir_stack[0])
            del dir_stack[0]

            logger.info(f'detecting funscripts from {current_dir}')

            if current_dir[-2:]=="/*":
                current_dir=current_dir[:-2]
                search_subdirectories=True
            else:
                search_subdirectories = False

            try:
                traversing_a_zip = True
                traversable = zipfile.Path(current_dir)
            except OSError:
                traversing_a_zip = False
                traversable = pathlib.Path(current_dir)

            for node in traversable.iterdir():
                full_path = os.path.join(current_dir, node.name)
                if not traversing_a_zip and node.is_dir(): # do not support dir-in-zip
                    if case_insensitive_compare(node.name, media_prefix):
                        new_dirs.append(full_path)
                    elif search_subdirectories:
                        new_dirs.append(full_path+"/*")
                else:
                    a, b, c = split_funscript_path(full_path)
                    if case_insensitive_compare(a, media_prefix):
                        if not traversing_a_zip and zipfile.is_zipfile(full_path):    # do not support zip-in-zip
                            new_dirs.append(full_path)
                        elif case_insensitive_compare(c, 'funscript'):
                            collected_files.append(Resource(node))


        except OSError as e:    # unreachable network?
            pass

        # make sure to search dirs before zipfiles
        new_zips = list(filter(path_is_zip, new_dirs))
        new_dirs = list(filter(lambda x: not path_is_zip(x), new_dirs))
        dir_stack = new_dirs + new_zips + dir_stack
        new_dirs = []



    return collected_files


def detect_variant_folders(media_path: str) -> list[tuple[str, str]]:
    """
    Look for a sibling `<media_prefix>_variants/` folder next to the media file.
    Returns an ordered list of (letter, absolute_path) for subfolders matching
    a single uppercase letter (A-Z). Empty if no variants folder exists.
    """
    if not media_path:
        return []
    dirname = os.path.dirname(media_path)
    basename = os.path.basename(media_path)
    media_prefix, _, _ = split_funscript_path(basename)
    if not media_prefix:
        return []
    variants_dir = os.path.join(dirname, f'{media_prefix}_variants')
    if not os.path.isdir(variants_dir):
        return []
    results = []
    try:
        for entry in sorted(os.listdir(variants_dir)):
            full = os.path.join(variants_dir, entry)
            if os.path.isdir(full) and VARIANT_FOLDER_PATTERN.match(entry):
                results.append((entry, full))
    except OSError:
        return []
    return results
