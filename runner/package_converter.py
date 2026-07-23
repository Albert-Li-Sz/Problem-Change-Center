from __future__ import annotations

import shutil

# Invokes this runner's fixed Python entrypoint without a shell.
import subprocess  # nosec B404
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

import format_bridge as bridge
from adapters import ADAPTERS, READABLE_FORMATS, WRITABLE_FORMATS
from package_adapters import choose_detected_format, detect_extracted
from package_ir import ProblemBundle, write_report
from package_repair import populate_repair_suggestions, repair_source_archive
from progress import ProgressReporter


REPORT_FILENAME = ".p2h-report.json"
MAX_NESTED_PACKAGES = 1000
EXPECTED_CONVERSION_ERRORS = (OSError, UnicodeError, ValueError, zipfile.BadZipFile)


def _copy_output(
    source: Path,
    destination: Path,
    *,
    reporter: ProgressReporter | None = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    files = [path for path in sorted(source.rglob("*")) if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files if not path.is_symlink())
    copied_bytes = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise ValueError(f"converter output contains a symbolic link: {relative}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as input_file, target.open("wb") as output_file:
                while chunk := input_file.read(1024 * 1024):
                    output_file.write(chunk)
                    copied_bytes += len(chunk)
                    if reporter is not None:
                        reporter.update(
                            current=copied_bytes,
                            total=total_bytes,
                            unit="bytes",
                            detail=f"copying {relative.as_posix()}",
                        )
            shutil.copystat(path, target, follow_symlinks=False)


def _ensure_convertible_with_report(
    bundle: ProblemBundle,
    *,
    target_format: str,
    loss_policy: str,
    output: Path,
    artifacts: list[str] | None = None,
) -> None:
    try:
        bundle.ensure_convertible(loss_policy)
    except ValueError:
        output.mkdir(parents=True, exist_ok=True)
        write_report(
            output / REPORT_FILENAME,
            bundle.report(target_format, artifacts or []),
        )
        raise


def _write_fatal_report_if_missing(
    output: Path,
    *,
    source_format: str,
    target_format: str,
    code: str,
    message: str,
) -> None:
    report_path = output / REPORT_FILENAME
    if report_path.exists():
        return
    output.mkdir(parents=True, exist_ok=True)
    failed = ProblemBundle(source_format, [])
    failed.add_issue("fatal", code, message, field="source")
    write_report(report_path, failed.report(target_format, []))


def _validate_writer_output(
    bundle: ProblemBundle, staged: Path, artifacts: list[str]
) -> None:
    if not artifacts or not any(path.is_file() for path in staged.rglob("*")):
        bundle.add_issue(
            "fatal",
            "missing-target-artifacts",
            "target writer completed without producing importable files",
            field="artifacts",
        )
    symlink = next((path for path in staged.rglob("*") if path.is_symlink()), None)
    if symlink is not None:
        bundle.add_issue(
            "fatal",
            "target-artifact-symlink",
            f"target writer produced a forbidden symbolic link: {symlink.relative_to(staged)}",
            field="artifacts",
        )


def _expand_nested_packages_for_detection(
    extracted: Path, budget: bridge.ArchiveExtractionBudget
) -> list[Any]:
    candidates = detect_extracted(extracted)
    if any(candidate.confidence >= 0.8 for candidate in candidates):
        return candidates
    nested_archives = [
        path
        for path in sorted(extracted.rglob("*.zip"))
        if path.is_file() and zipfile.is_zipfile(path)
    ]
    if len(nested_archives) > MAX_NESTED_PACKAGES:
        raise ValueError(f"nested package count exceeds {MAX_NESTED_PACKAGES}")
    nested_root = extracted / ".p2h-nested"
    for index, archive in enumerate(nested_archives, start=1):
        destination = nested_root / f"{index:04d}"
        bridge._safe_extract_zip(archive, destination, budget=budget)
        for suffix in (
            "statement.md",
            "statement.html",
            "statement.pdf",
            "metadata.json",
        ):
            sidecar = archive.with_name(f"{archive.stem}.{suffix}")
            if sidecar.is_file():
                bridge._copy_file(sidecar, destination / suffix)
    return detect_extracted(extracted)


def _normalize_single_polygon_package(
    source_zip: Path,
    extracted: Path,
    workspace: Path,
) -> Path:
    if any(
        path.is_file()
        and len(path.relative_to(extracted).parts) >= 3
        and path.relative_to(extracted).parts[-3] == "problems"
        for path in extracted.rglob("problem.xml")
    ):
        return source_zip
    problem_xmls = [
        path
        for path in sorted(extracted.rglob("problem.xml"))
        if path.is_file()
        and "/problems/" not in f"/{path.relative_to(extracted).as_posix()}"
    ]
    if len(problem_xmls) != 1:
        return source_zip
    problem_root = problem_xmls[0].parent
    slug_source = problem_root.name if problem_root != extracted else source_zip.stem
    slug = bridge._safe_name(slug_source) or "problem"
    normalized = workspace / "single-polygon.zip"
    with zipfile.ZipFile(normalized, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(problem_root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(problem_root).as_posix()
                archive.write(path, f"problems/{slug}/{relative}")
    return normalized


def _polygon_command(
    source_zip: Path,
    output: Path,
    *,
    target: str,
    options: dict[str, Any],
    only: Iterable[str],
) -> list[str]:
    script = Path(__file__).with_name("p2h_safe.py")
    if target == "icpc":
        command = [
            sys.executable,
            str(script),
            "domjudge-convert",
            str(source_zip),
            "-o",
            str(output),
            "--code-start",
            str(options.get("code_start") or "A"),
            "--color",
            str(options.get("color") or "#000000"),
            "--missing-env",
            str(options.get("missing_env") or "warn"),
            "--verbose",
        ]
        if options.get("with_statement"):
            command.append("--with-statement")
        if options.get("with_attachments"):
            command.append("--with-attachments")
        validator_mode = str(options.get("validator_mode") or "auto")
        if validator_mode == "default":
            command.append("--default-validator")
        elif validator_mode == "auto":
            command.append("--auto-validator")
    else:
        command = [
            sys.executable,
            str(script),
            "convert",
            str(source_zip),
            "-o",
            str(output),
            "--pid-start",
            str(options.get("pid_start") or "P1000"),
            "--owner",
            str(options.get("owner") or 1),
            "--missing-env",
            str(options.get("missing_env") or "warn"),
            "--verbose",
        ]
        for tag in options.get("tags", []):
            command.extend(["--tag", str(tag)])
    for slug in only:
        command.extend(["--only", str(slug)])
    command.append("--run-doall" if options.get("run_doall") else "--no-run-doall")
    return command


def _run_polygon_route(
    source_zip: Path,
    output: Path,
    *,
    target_format: str,
    loss_policy: str,
    only: Iterable[str],
    options: dict[str, Any],
    workspace: Path,
    reporter: ProgressReporter | None = None,
) -> dict[str, Any]:
    reporter = reporter or ProgressReporter(output_format="none")
    return_direct_icpc = (
        target_format == "icpc"
        and str(options.get("profile") or "legacy-icpc") != "2025-09"
    )
    direct_target = "icpc" if target_format == "icpc" else "hydro"
    polygon_output = workspace / "polygon-output"
    polygon_output.mkdir()
    reporter.phase(
        "write",
        detail=f"running Polygon optimized route to {direct_target}",
    )
    # The argv is internally constructed and shell=False is retained.
    result = subprocess.run(  # nosec B603
        _polygon_command(
            source_zip, polygon_output, target=direct_target, options=options, only=only
        ),
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"Polygon converter exited with code {result.returncode}")
    if target_format == "hydro" or return_direct_icpc:
        artifacts = sorted(
            path.relative_to(polygon_output).as_posix()
            for path in polygon_output.rglob("*")
            if path.is_file()
        )
        problem_count = sum(artifact.lower().endswith(".zip") for artifact in artifacts)
        if problem_count == 0:
            failed = ProblemBundle("polygon", [])
            failed.add_issue(
                "fatal",
                "missing-converter-output",
                "Polygon converter completed without producing any problem package; verify the selected problem names and source package contents",
                field="artifacts",
            )
            _ensure_convertible_with_report(
                failed,
                target_format=target_format,
                loss_policy=loss_policy,
                output=output,
            )
        report = ProblemBundle("polygon", []).report(target_format, artifacts)
        report["problem_count"] = problem_count
        reporter.complete(detail=f"Polygon produced {problem_count} problem packages")
        reporter.phase("validate_output", detail="validating Polygon output")
        _copy_output(polygon_output, output, reporter=reporter)
        reporter.complete(detail="Polygon output validated")
        write_report(output / REPORT_FILENAME, report)
        return report

    intermediate_root = workspace / f"polygon-{direct_target}"
    intermediate_root.mkdir()
    budget = bridge.ArchiveExtractionBudget.from_env()
    for index, package in enumerate(sorted(polygon_output.glob("*.zip")), start=1):
        bridge._safe_extract_zip(
            package, intermediate_root / f"{index:04d}", budget=budget
        )
    bundle = ADAPTERS[direct_target].read(
        intermediate_root, workspace / "polygon-ir", (), budget, reporter
    )
    bundle.source_format = "polygon"
    reporter.phase(
        "validate_ir",
        detail="validating Polygon intermediate representation",
        total=len(bundle.problems),
        unit="problems",
    )
    bundle.validate_integrity()
    populate_repair_suggestions(bundle, intermediate_root)
    reporter.complete(detail="Polygon intermediate representation validated")
    _ensure_convertible_with_report(
        bundle,
        target_format=target_format,
        loss_policy=loss_policy,
        output=output,
    )
    writer = ADAPTERS[target_format].write
    if writer is None:
        raise ValueError(f"target format is read-only: {target_format}")
    validator = ADAPTERS[target_format].validate_target
    if validator is not None:
        validator(bundle, options)
    _ensure_convertible_with_report(
        bundle,
        target_format=target_format,
        loss_policy=loss_policy,
        output=output,
    )
    staged = workspace / "target-output"
    staged.mkdir()
    options["_progress"] = reporter
    reporter.phase(
        "write",
        detail=f"writing {target_format} packages",
        total=len(bundle.problems),
        unit="problems",
    )
    try:
        artifacts = writer(bundle, staged, options)
    except EXPECTED_CONVERSION_ERRORS as exc:
        bundle.add_issue(
            "fatal",
            "target-write-error",
            f"failed to write {target_format} package: {exc}",
            field="artifacts",
        )
        _ensure_convertible_with_report(
            bundle,
            target_format=target_format,
            loss_policy=loss_policy,
            output=output,
        )
        raise
    reporter.complete(detail=f"wrote {len(artifacts)} target artifacts")
    reporter.phase("validate_output", detail="validating target artifacts")
    _validate_writer_output(bundle, staged, artifacts)
    _ensure_convertible_with_report(
        bundle,
        target_format=target_format,
        loss_policy=loss_policy,
        output=output,
        artifacts=artifacts,
    )
    _copy_output(staged, output, reporter=reporter)
    reporter.complete(detail="target artifacts validated")
    report = bundle.report(target_format, artifacts)
    write_report(output / REPORT_FILENAME, report)
    return report


def convert_package(
    source_zip: Path,
    output: Path,
    *,
    source_format: str = "auto",
    target_format: str,
    loss_policy: str = "warn",
    only: Iterable[str] = (),
    options: dict[str, Any] | None = None,
    reporter: ProgressReporter | None = None,
    repair_plan: Path | None = None,
    supplements_dir: Path | None = None,
) -> dict[str, Any]:
    progress = reporter or ProgressReporter(output_format="none")
    if source_format not in {"auto", "polygon", *READABLE_FORMATS}:
        raise ValueError(f"unsupported source format: {source_format}")
    if target_format not in WRITABLE_FORMATS:
        raise ValueError(f"unsupported target format: {target_format}")
    if loss_policy not in {"warn", "error"}:
        raise ValueError("loss_policy must be warn or error")
    if not source_zip.is_file():
        raise ValueError("input ZIP does not exist")
    progress.phase("validate_archive", detail="validating source ZIP archive")
    try:
        bridge.validate_zip_archive(source_zip)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        _write_fatal_report_if_missing(
            output,
            source_format=source_format,
            target_format=target_format,
            code="invalid-source-archive",
            message=f"input is not a valid safe ZIP archive: {exc}",
        )
        raise ValueError(f"input is not a valid safe ZIP archive: {exc}") from exc
    progress.complete(detail="source ZIP archive is safe")
    options = dict(options or {})
    with tempfile.TemporaryDirectory(prefix="package-convert-") as temporary:
        workspace = Path(temporary)
        applied_repairs: list[dict[str, str]] = []
        if repair_plan is not None:
            repaired_zip = workspace / "repaired-source.zip"
            progress.phase("validate_archive", detail="applying confirmed repair plan")
            applied_repairs = repair_source_archive(
                source_zip,
                repaired_zip,
                plan_path=repair_plan,
                supplements_dir=supplements_dir,
                workspace=workspace,
            )
            source_zip = repaired_zip
            progress.complete(
                detail=f"applied {len(applied_repairs)} confirmed repairs"
            )
        extracted = workspace / "input"
        extraction_budget = bridge.ArchiveExtractionBudget.from_env()
        progress.phase("extract", detail="safely extracting source package")
        try:
            bridge._safe_extract_zip(source_zip, extracted, budget=extraction_budget)
            progress.complete(detail="source package extracted")
            progress.phase("detect", detail="detecting source package format")
            candidates = _expand_nested_packages_for_detection(
                extracted, extraction_budget
            )
            progress.complete(detail="source package format candidates collected")
        except EXPECTED_CONVERSION_ERRORS as exc:
            _write_fatal_report_if_missing(
                output,
                source_format=source_format,
                target_format=target_format,
                code="source-extraction-error",
                message=f"failed to safely extract source package: {exc}",
            )
            raise
        if source_format == "auto":
            try:
                detected = choose_detected_format(candidates)
            except ValueError as exc:
                _write_fatal_report_if_missing(
                    output,
                    source_format="auto",
                    target_format=target_format,
                    code="source-detection-error",
                    message=str(exc),
                )
                raise
        else:
            detected = source_format
        if source_format != "auto":
            strong = {
                candidate.format
                for candidate in candidates
                if candidate.confidence >= 0.8
            }
            if strong and source_format not in strong and source_format != "generic":
                message = (
                    f"source format mismatch: requested {source_format}, "
                    f"detected {', '.join(sorted(strong))}"
                )
                _write_fatal_report_if_missing(
                    output,
                    source_format=source_format,
                    target_format=target_format,
                    code="source-format-mismatch",
                    message=message,
                )
                raise ValueError(message)
        if detected == target_format:
            message = "source and target formats must differ"
            _write_fatal_report_if_missing(
                output,
                source_format=detected,
                target_format=target_format,
                code="source-target-same",
                message=message,
            )
            raise ValueError(message)
        if detected == "polygon":
            try:
                polygon_source = _normalize_single_polygon_package(
                    source_zip, extracted, workspace
                )
                report = _run_polygon_route(
                    polygon_source,
                    output,
                    target_format=target_format,
                    loss_policy=loss_policy,
                    only=only,
                    options=options,
                    workspace=workspace,
                    reporter=progress,
                )
                report["applied_repairs"] = applied_repairs
                write_report(output / REPORT_FILENAME, report)
                return report
            except EXPECTED_CONVERSION_ERRORS as exc:
                _write_fatal_report_if_missing(
                    output,
                    source_format="polygon",
                    target_format=target_format,
                    code="polygon-conversion-error",
                    message=str(exc),
                )
                raise
        adapter = ADAPTERS.get(detected)
        if adapter is None:
            raise ValueError(f"no reader for source format: {detected}")
        progress.phase("read", detail=f"reading {detected} package")
        try:
            bundle = adapter.read(
                extracted,
                workspace / "reader",
                only,
                extraction_budget,
                progress,
            )
        except EXPECTED_CONVERSION_ERRORS as exc:
            _write_fatal_report_if_missing(
                output,
                source_format=detected,
                target_format=target_format,
                code="source-read-error",
                message=f"failed to read {detected} package: {exc}",
            )
            raise
        bundle.applied_repairs = applied_repairs
        progress.complete(
            detail=f"read {len(bundle.problems)} problems from {detected}"
        )
        progress.phase(
            "validate_ir",
            detail="validating intermediate representation",
            total=len(bundle.problems),
            unit="problems",
        )
        bundle.validate_integrity()
        populate_repair_suggestions(bundle, extracted)
        progress.complete(detail="intermediate representation validated")
        _ensure_convertible_with_report(
            bundle,
            target_format=target_format,
            loss_policy=loss_policy,
            output=output,
        )
        writer = ADAPTERS[target_format].write
        if writer is None:
            raise ValueError(f"target format is read-only: {target_format}")
        validator = ADAPTERS[target_format].validate_target
        if validator is not None:
            validator(bundle, options)
        _ensure_convertible_with_report(
            bundle,
            target_format=target_format,
            loss_policy=loss_policy,
            output=output,
        )
        staged = workspace / "output"
        staged.mkdir()
        options["_progress"] = progress
        progress.phase(
            "write",
            detail=f"writing {target_format} packages",
            total=len(bundle.problems),
            unit="problems",
        )
        try:
            artifacts = writer(bundle, staged, options)
        except EXPECTED_CONVERSION_ERRORS as exc:
            bundle.add_issue(
                "fatal",
                "target-write-error",
                f"failed to write {target_format} package: {exc}",
                field="artifacts",
            )
            _ensure_convertible_with_report(
                bundle,
                target_format=target_format,
                loss_policy=loss_policy,
                output=output,
            )
            raise
        progress.complete(detail=f"wrote {len(artifacts)} target artifacts")
        progress.phase("validate_output", detail="validating target artifacts")
        _validate_writer_output(bundle, staged, artifacts)
        _ensure_convertible_with_report(
            bundle,
            target_format=target_format,
            loss_policy=loss_policy,
            output=output,
            artifacts=artifacts,
        )
        report = bundle.report(target_format, artifacts)
        _copy_output(staged, output, reporter=progress)
        progress.complete(detail="target artifacts validated")
        write_report(output / REPORT_FILENAME, report)
        return report
