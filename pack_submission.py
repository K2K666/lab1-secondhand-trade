"""Assemble the course ZIP using the student's completed PDF."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
TITLE = "实验3_241240048_康子凯"
CODE_SUFFIXES = {".py", ".sql", ".js", ".css", ".html", ".svg", ".toml"}


def files_for_submission(report):
    diagram_sources = sorted((ROOT / "UML").glob("*.puml"))
    if len(diagram_sources) != 4:
        raise ValueError("需有四份 PlantUML 源文件")
    yield "实验报告.pdf", report
    for source in diagram_sources:
        picture = source.with_suffix(".png")
        if not picture.is_file():
            raise ValueError(f"缺少 UML 图片 {picture.name}")
        for file in (source, picture):
            yield file.relative_to(ROOT).as_posix(), file
    for file in sorted((ROOT / "code").rglob("*")):
        path = file.relative_to(ROOT)
        if (
            file.is_file()
            and not {"var", "__pycache__", "dist"}.intersection(path.parts)
            and file.suffix in CODE_SUFFIXES
        ):
            yield path.as_posix(), file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT.parent / f"{TITLE}.zip")
    args = parser.parse_args()
    report, output = args.report.resolve(), args.output.resolve()
    if output.suffix.lower() != ".zip" or output == report:
        parser.error("输出必须为独立的 .zip 文件")
    try:
        with report.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("请提供有效的实验报告 PDF")
        files = list(files_for_submission(report))
        pending = output.with_suffix(".zip.pending")
        output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(pending, "w", ZIP_DEFLATED) as archive:
            for name, source in files:
                archive.write(source, f"{TITLE}/{name}")
        with ZipFile(pending) as archive:
            if archive.testzip() or len(archive.namelist()) != len(files):
                raise ValueError("压缩包校验未通过")
        pending.replace(output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(output)


if __name__ == "__main__":
    main()
