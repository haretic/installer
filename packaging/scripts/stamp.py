"""Write haretic_installer/_build.py: the version and whether this build is portable."""
import pathlib
import sys

version, portable = sys.argv[1], sys.argv[2] == '1'
root = pathlib.Path(__file__).resolve().parents[2]
(root / 'haretic_installer/_build.py').write_text(f'VERSION = {version!r}\nPORTABLE = {portable}\n')
