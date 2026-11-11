"""Build checks, source line counting, and optional runtime archive."""

import argparse
import py_compile
import shutil
import subprocess
import sys
import tokenize
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
APP = ROOT / "campus_trade"
EXCLUDED_TOKENS = {
    tokenize.COMMENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.NEWLINE,
    tokenize.NL,
}


def _count_python(path):
    lines = set()
    with path.open("rb") as stream:
        for token in tokenize.tokenize(stream.readline):
            if token.type not in EXCLUDED_TOKENS:
                lines.update(range(token.start[0], token.end[0] + 1))
    return len(lines)


def _count_plain(path, comment):
    return sum(
        bool(line.strip()) and not line.lstrip().startswith(comment)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
    )


def _print_size():
    python_lines = sum(_count_python(path) for path in APP.rglob("*.py"))
    sql_lines = sum(_count_plain(path, "--") for path in APP.glob("*.sql"))
    js_lines = sum(_count_plain(path, "//") for path in (APP / "static").glob("*.js"))
    print(f"Application source: Python {python_lines}, SQL {sql_lines}, JavaScript {js_lines}")
    print(
        f"Total source lines (excluding tests, HTML, CSS): {python_lines + sql_lines + js_lines}"
    )
    if python_lines + sql_lines + js_lines <= 1500:
        raise SystemExit("Source line count must exceed 1500")


def _compile():
    files = sorted([*APP.rglob("*.py"), *(ROOT / "tests").rglob("*.py")])
    with TemporaryDirectory() as directory:
        for index, path in enumerate(files):
            py_compile.compile(str(path), str(Path(directory) / f"{index}.pyc"), doraise=True)
    print(f"Python compilation succeeded ({len(files)} files)")
    node = shutil.which("node")
    if node:
        for path in sorted((APP / "static").glob("*.js")):
            subprocess.run([node, "--check", str(path)], check=True)
        print("JavaScript syntax check succeeded")
    else:
        print("JavaScript syntax check requires Node.js")


def _archive(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [
        path
        for path in APP.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Runtime archive could not be verified")
    print(f"Runtime archive: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    _compile()
    _print_size()
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT, check=True
    )
    if args.archive:
        _archive(args.archive.resolve())


if __name__ == "__main__":
    main()
