#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
            "checker: checker.cpp\nvalidator: validator.cpp\n"
            "cases:\n  - input: 1.in\n    output: 1.ans\n"
        ),
        "P1002/testdata/1.in": "1\n",
        "P1002/testdata/1.ans": "1\n",
        "P1002/testdata/checker.cpp": (
            '#include "testlib.h"\n'
            "int main(int argc,char** argv){registerTestlibCmd(argc,argv);"
            'quitf(_ok,"ok");}\n'
        ),
        "P1002/testdata/validator.cpp": (
            '#include "testlib.h"\n'
            "int main(int argc,char** argv){registerValidation(argc,argv);"
            "inf.readInt();inf.readEof();}\n"
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
    _write_archive(
        destination / "hydro-case-only-answer.zip",
        {
            "P2001/problem.yaml": "title: Case-only answer\npid: P2001\n",
            "P2001/problem_en.md": "# Case-only answer\n",
            "P2001/testdata/config.yaml": (
                "cases:\n  - input: 1.in\n    output: Answer.ANS\n"
            ),
            "P2001/testdata/1.in": "4 5\n",
            "P2001/testdata/answer.ans": "9\n",
        },
    )
    _write_archive(
        destination / "hydro-missing-validator.zip",
        {
            "P2002/problem.yaml": "title: Missing validator\npid: P2002\n",
            "P2002/problem_en.md": "# Missing validator\n",
            "P2002/testdata/config.yaml": (
                "validator: validator.cpp\ncases:\n  - input: 1.in\n    output: 1.ans\n"
            ),
            "P2002/testdata/1.in": "1\n",
            "P2002/testdata/1.ans": "1\n",
        },
    )
    _write_archive(
        destination / "hydro-answer-conflict.zip",
        {
            "P2003/problem.yaml": "title: Conflicting answers\npid: P2003\n",
            "P2003/problem_en.md": "# Conflicting answers\n",
            "P2003/testdata/config.yaml": (
                "cases:\n  - input: 1.in\n    output: 1.ans\n"
            ),
            "P2003/testdata/1.in": "2 2\n",
            "P2003/testdata/1.ans": "4\n",
            "P2003/testdata/1.out": "5\n",
        },
    )
    _write_archive(
        destination / "generic-multi.zip",
        {
            "最大值/statement.md": "# 最大值\n",
            "最大值/1.in": "1 3\n",
            "最大值/1.out": "3\n",
            "sum/statement.md": "# Sum\n",
            "sum/1.in": "2 3\n",
            "sum/1.ans": "5\n",
        },
    )
    _write_archive(
        destination / "icpc-minimal.zip",
        {
            "problem.yaml": (
                "problem_format_version: legacy-icpc\n"
                "name:\n  en: Sum\n  zh: 求和\n"
                "validation: default\n"
                "limits:\n  time_limit: 1\n  memory: 256\n"
            ),
            "domjudge-problem.ini": "short-name = sum\ntimelimit = 1\n",
            "problem_statement/problem.en.md": "# Sum\n\nAdd two integers.\n",
            "problem_statement/problem.zh.md": "# 求和\n\n计算两个整数之和。\n",
            "data/sample/1.in": "1 2\n",
            "data/sample/1.ans": "3\n",
            "data/secret/1.in": "4 5\n",
            "data/secret/1.ans": "9\n",
        },
    )
    _write_archive(
        destination / "fps-minimal.zip",
        {
            "problem.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<fps version="1.6"><item>'
                "<title>Sum</title><remote_id>sum</remote_id>"
                "<description><![CDATA[Add two integers.]]></description>"
                "<input><![CDATA[Two integers.]]></input>"
                "<output><![CDATA[Their sum.]]></output>"
                '<time_limit unit="s">1</time_limit>'
                '<memory_limit unit="mb">256</memory_limit>'
                "<sample_input>1 2</sample_input><sample_output>3</sample_output>"
                '<test_input name="1">4 5</test_input><test_output>9</test_output>'
                '<template language="cpp">int main(){}</template>'
                "</item></fps>\n"
            )
        },
    )
    manifest = {
        "schema_version": 1,
        "license": "CC0-1.0",
        "fixtures": [
            {
                "path": "hydro-core-rich.zip",
                "source_format": "hydro",
                "targets": ["icpc", "hoj"],
                "problem_count": 5,
                "capabilities": [
                    "attachments",
                    "checker",
                    "file-io",
                    "interactive",
                    "multilingual-statements",
                    "pdf-statements",
                    "subtask-dependencies",
                    "templates",
                    "validator",
                ],
            },
            {
                "path": "icpc-minimal.zip",
                "source_format": "icpc",
                "targets": ["hydro"],
                "problem_count": 1,
                "capabilities": ["multilingual-statements", "samples"],
            },
            {
                "path": "fps-minimal.zip",
                "source_format": "fps",
                "targets": ["hydro"],
                "problem_count": 1,
                "capabilities": ["samples", "templates"],
            },
            {
                "path": "generic-multi.zip",
                "source_format": "generic",
                "targets": ["hydro"],
                "problem_count": 2,
                "capabilities": ["multiple-problems", "unicode-paths"],
            },
            {
                "path": "hydro-missing-answer.zip",
                "source_format": "hydro",
                "invalid_target": "hoj",
                "fatal_codes": ["hydro-missing-test-output"],
                "repair_strategy": "extension-alias",
            },
            {
                "path": "hydro-case-only-answer.zip",
                "source_format": "hydro",
                "invalid_target": "hoj",
                "fatal_codes": ["hydro-missing-test-output"],
                "repair_strategy": "case-only",
                "requires_case_sensitive_fs": True,
            },
            {
                "path": "hydro-missing-validator.zip",
                "source_format": "hydro",
                "invalid_target": "icpc",
                "fatal_codes": ["hydro-missing-validator"],
                "repair_strategy": "upload",
            },
            {
                "path": "hydro-answer-conflict.zip",
                "source_format": "hydro",
                "invalid_target": "hoj",
                "fatal_codes": ["hydro-unpaired-test-file"],
            },
        ],
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build redistributable OJ fixtures")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.destination)
    print(f"compatibility corpus written to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
