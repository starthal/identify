import errno
import math
import os.path
import re
import shlex
import stat
import string
import sys
from typing import IO
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

from identify import extensions
from identify import interpreters
from identify.vendor import licenses


printable = frozenset(string.printable)

DIRECTORY = 'directory'
SYMLINK = 'symlink'
SOCKET = 'socket'
FILE = 'file'
EXECUTABLE = 'executable'
NON_EXECUTABLE = 'non-executable'
TEXT = 'text'
BINARY = 'binary'

TYPE_TAGS = frozenset((DIRECTORY, FILE, SYMLINK, SOCKET))
MODE_TAGS = frozenset((EXECUTABLE, NON_EXECUTABLE))
ENCODING_TAGS = frozenset((BINARY, TEXT))
_ALL_TAGS = {*TYPE_TAGS, *MODE_TAGS, *ENCODING_TAGS}
_ALL_TAGS.update(*extensions.EXTENSIONS.values())
_ALL_TAGS.update(*extensions.EXTENSIONS_NEED_BINARY_CHECK.values())
_ALL_TAGS.update(*extensions.NAMES.values())
_ALL_TAGS.update(*interpreters.INTERPRETERS.values())
ALL_TAGS = frozenset(_ALL_TAGS)


def tags_from_path(path: str) -> Set[str]:
    try:
        sr = os.lstat(path)
    except (OSError, ValueError):  # same error-handling as `os.lexists()`
        raise ValueError(f'{path} does not exist.')

    mode = sr.st_mode
    if stat.S_ISDIR(mode):
        return {DIRECTORY}
    if stat.S_ISLNK(mode):
        return {SYMLINK}
    if stat.S_ISSOCK(mode):
        return {SOCKET}

    tags = {FILE}

    executable = os.access(path, os.X_OK)
    if executable:
        tags.add(EXECUTABLE)
    else:
        tags.add(NON_EXECUTABLE)

    # As an optimization, if we're able to read tags from the filename, then we
    # don't peek at the file contents.
    t = tags_from_filename(os.path.basename(path))
    if len(t) > 0:
        tags.update(t)
    else:
        if executable:
            shebang = parse_shebang_from_file(path)
            if len(shebang) > 0:
                tags.update(tags_from_interpreter(shebang[0]))

    # some extensions can be both binary and text
    # see EXTENSIONS_NEED_BINARY_CHECK
    if not ENCODING_TAGS & tags:
        if file_is_text(path):
            tags.add(TEXT)
        else:
            tags.add(BINARY)

    assert ENCODING_TAGS & tags, tags
    assert MODE_TAGS & tags, tags
    return tags


def tags_from_filename(path: str) -> Set[str]:
    _, filename = os.path.split(path)
    _, ext = os.path.splitext(filename)

    ret = set()

    # Allow e.g. "Dockerfile.xenial" to match "Dockerfile"
    for part in [filename] + filename.split('.'):
        if part in extensions.NAMES:
            ret.update(extensions.NAMES[part])
            break

    if len(ext) > 0:
        ext = ext[1:].lower()
        if ext in extensions.EXTENSIONS:
            ret.update(extensions.EXTENSIONS[ext])
        elif ext in extensions.EXTENSIONS_NEED_BINARY_CHECK:
            ret.update(extensions.EXTENSIONS_NEED_BINARY_CHECK[ext])

    return ret


def tags_from_interpreter(interpreter: str) -> Set[str]:
    _, _, interpreter = interpreter.rpartition('/')

    # Try "python3.5.2" => "python3.5" => "python3" until one matches.
    while interpreter:
        if interpreter in interpreters.INTERPRETERS:
            return interpreters.INTERPRETERS[interpreter]
        else:
            interpreter, _, _ = interpreter.rpartition('.')

    return set()


def is_text(bytesio: IO[bytes]) -> bool:
    """Return whether the first KB of contents seems to be binary.

    This is roughly based on libmagic's binary/text detection:
    https://github.com/file/file/blob/df74b09b9027676088c797528edcaae5a9ce9ad0/src/encoding.c#L203-L228
    """
    text_chars = (
        bytearray([7, 8, 9, 10, 11, 12, 13, 27]) +
        bytearray(range(0x20, 0x7F)) +
        bytearray(range(0x80, 0X100))
    )
    return not bool(bytesio.read(1024).translate(None, text_chars))


def file_is_text(path: str) -> bool:
    if not os.path.lexists(path):
        raise ValueError(f'{path} does not exist.')
    with open(path, 'rb') as f:
        return is_text(f)


def _shebang_split(line: str) -> List[str]:
    try:
        # shebangs aren't supposed to be quoted, though some tools such as
        # setuptools will write them with quotes so we'll best-guess parse
        # with shlex first
        return shlex.split(line)
    except ValueError:
        # failing that, we'll do a more "traditional" shebang parsing which
        # just involves splitting by whitespace
        return line.split()


def _parse_nix_shebang(
        bytesio: IO[bytes],
        cmd: Tuple[str, ...],
) -> Tuple[str, ...]:
    while bytesio.read(2) == b'#!':
        next_line_b = bytesio.readline()
        try:
            next_line = next_line_b.decode('UTF-8')
        except UnicodeDecodeError:
            return cmd

        for c in next_line:
            if c not in printable:
                return cmd

        line_tokens = tuple(_shebang_split(next_line.strip()))
        for i, token in enumerate(line_tokens[:-1]):
            if token != '-i':
                continue
            # the argument to -i flag
            cmd = (line_tokens[i + 1],)
    return cmd


def parse_shebang(bytesio: IO[bytes]) -> Tuple[str, ...]:
    """Parse the shebang from a file opened for reading binary."""
    if bytesio.read(2) != b'#!':
        return ()
    first_line_b = bytesio.readline()
    try:
        first_line = first_line_b.decode('UTF-8')
    except UnicodeDecodeError:
        return ()

    # Require only printable ascii
    for c in first_line:
        if c not in printable:
            return ()

    cmd = tuple(_shebang_split(first_line.strip()))
    if cmd and cmd[0] == '/usr/bin/env':
        if cmd[1] == '-S':
            cmd = cmd[2:]
        else:
            cmd = cmd[1:]

        if cmd == ('nix-shell',):
            return _parse_nix_shebang(bytesio, cmd)
    return cmd


def parse_shebang_from_file(path: str) -> Tuple[str, ...]:
    """Parse the shebang given a file path."""
    if not os.path.lexists(path):
        raise ValueError(f'{path} does not exist.')
    if not os.access(path, os.X_OK):
        return ()

    try:
        with open(path, 'rb') as f:
            return parse_shebang(f)
    except OSError as e:
        if e.errno == errno.EINVAL:
            return ()
        else:
            raise


COPYRIGHT_RE = re.compile(r'^\s*(Copyright|\(C\)) .*$', re.I | re.MULTILINE)
WS_RE = re.compile(r'\s+')


def _norm_license(s: str) -> str:
    s = COPYRIGHT_RE.sub('', s)
    s = WS_RE.sub(' ', s)
    return s.strip()


def license_id(filename: str) -> Optional[str]:
    """Return the spdx id for the license contained in `filename`.  If no
    license is detected, returns `None`.

    spdx: https://spdx.org/licenses/
    licenses from choosealicense.com: https://github.com/choosealicense.com

    Approximate algorithm:

    1. strip copyright line
    2. normalize whitespace (replace all whitespace with a single space)
    3. check exact text match with existing licenses
    4. failing that use edit distance
    """
    import ukkonen  # `pip install identify[license]`

    with open(filename, encoding='UTF-8') as f:
        contents = f.read()

    norm = _norm_license(contents)

    min_edit_dist = sys.maxsize
    min_edit_dist_spdx = ''

    cutoff = math.ceil(.05 * len(norm))

    # try exact matches
    for spdx, text in licenses.LICENSES:
        norm_license = _norm_license(text)
        if norm == norm_license:
            return spdx

        # skip the slow calculation if the lengths are very different
        if norm and abs(len(norm) - len(norm_license)) / len(norm) > .05:
            continue

        edit_dist = ukkonen.distance(norm, norm_license, cutoff)
        if edit_dist < cutoff and edit_dist < min_edit_dist:
            min_edit_dist = edit_dist
            min_edit_dist_spdx = spdx

    # if there's less than 5% edited from the license, we found our match
    if norm and min_edit_dist < cutoff:
        return min_edit_dist_spdx
    else:
        # no matches :'(
        return None
