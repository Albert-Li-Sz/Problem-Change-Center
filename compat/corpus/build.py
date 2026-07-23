#!/usr/bin/env python3
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _write_archive(path: Path, files: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info, content.encode("utf-8") if isinstance(content, str) else content
            )


def _core_files() -> dict[str, str | bytes]:
    common_solution = (
        "#include <iostream>\n"
        "int main(){long long a,b;if(std::cin>>a>>b)std::cout<<a+b<<'\\n';}\n"
    )
    files: dict[str, str | bytes] = {
        "P1000/problem.yaml": "title: A + B\npid: P1000\ntag: [compat, acm]\n",
        "P1000/problem_en.md": "# A + B\n\nAdd two integers.\n",
        "P1000/problem_zh.md": "# A + B\n\n计算两个整数之和。\n",
        "P1000/testdata/config.yaml": (
            "type: default\ntime: 1500ms\nmemory: 256m\n"
            "filename: sum\n"
            "cases:\n  - input: 1.in\n    output: 1.ans\n"
        ),
        "P1000/testdata/1.in": "1 2\n",
        "P1000/testdata/1.ans": "3\n",
        "P1000/testdata/solution.cpp": common_solution,
        "P1000/additional_file/samples/1.in": "2 3\n",
        "P1000/additional_file/samples/1.out": "5\n",
        "P1001/problem.yaml": "title: OI groups\npid: P1001\ntag: [compat, oi]\n",
        "P1001/problem_en.md": "# OI groups\n\nTwo dependent subtasks.\n",
        "P1001/testdata/config.yaml": (
            "type: objective\ntime: 1s\nmemory: 128m\n"
            "subtasks:\n"
            "  - name: base\n    score: 40\n"
            "    cases:\n      - input: 1.in\n        output: 1.ans\n"
            "  - name: full\n    score: 60\n    dependencies: [base]\n"
            "    cases:\n      - input: 2.in\n        output: 2.ans\n"
        ),
        "P1001/testdata/1.in": "1\n",
        "P1001/testdata/1.ans": "1\n",
        "P1001/testdata/2.in": "2\n",
        "P1001/testdata/2.ans": "2\n",
        "P1002/problem.yaml": "title: Custom checker\npid: P1002\n",
        "P1002/problem_en.md": "# Custom checker\n\nWhitespace-insensitive output.\n",
        "P1002/testdata/config.yaml": (
            "checker: checker.cpp\ncases:\n  - input: 1.in\n    output: 1.ans\n"
        ),
        "P1002/testdata/1.in": "1\n",
        "P1002/testdata/1.ans": "1\n",
        "P1002/testdata/checker.cpp": (
            '#include "testlib.h"\n'
            "int main(int argc,char** argv){registerTestlibCmd(argc,argv);"
            'quitf(_ok,"ok");}\n'
        ),
        "P1003/problem.yaml": "title: Interactive echo\npid: P1003\n",
        "P1003/problem_en.md": "# Interactive echo\n\nReply with the received value.\n",
        "P1003/testdata/config.yaml": (
            "type: interactive\ninteractor: interactor.cpp\n"
            "cases:\n  - input: 1.in\n    output: 1.ans\n"
        ),
        "P1003/testdata/1.in": "7\n",
        "P1003/testdata/1.ans": "7\n",
        "P1003/testdata/interactor.cpp": "int main(){return 0;}\n",
        "P1004/problem.yaml": "title: PDF assets\npid: P1004\ntag: [compat, pdf]\n",
        "P1004/problem_en.md": "@[pdf](file://statement-en.pdf)\n",
        "P1004/problem_zh.md": "@[pdf](file://statement-zh.pdf)\n",
        "P1004/additional_file/statement-en.pdf": PDF,
        "P1004/additional_file/statement-zh.pdf": PDF,
        "P1004/additional_file/attachments/notes.txt": "public attachment\n",
        "P1004/additional_file/code_templates.json": (
            '[{"language":"cpp","code":"#include <bits/stdc++.h>\\nint main(){}\\n"}]\n'
        ),
        "P1004/additional_file/sources/submissions/accepted/main.cpp": common_solution,
        "P1004/testdata/config.yaml": ("cases:\n  - input: 1.in\n    output: 1.ans\n"),
        "P1004/testdata/1.in": "4 5\n",
        "P1004/testdata/1.ans": "9\n",
    }
    return files


def build(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    _write_archive(destination / "hydro-core-rich.zip", _core_files())
    missing = {
        "P2000/problem.yaml": "title: Missing answer\npid: P2000\n",
        "P2000/problem_en.md": "# Missing answer\n",
        "P2000/testdata/config.yaml": ("cases:\n  - input: 1.in\n    output: 1.ans\n"),
        "P2000/testdata/1.in": "1 2\n",
        "P2000/testdata/1.out": "3\n",
    }
    _write_archive(destination / "hydro-missing-answer.zip", missing)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build redistributable OJ fixtures")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.destination)
    print(f"compatibility corpus written to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
