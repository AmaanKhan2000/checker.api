"""Atomic JSON and JUnit outputs for CI systems."""

import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from api_sentinel.models import RunReport


def atomic_write(path: Path, contents: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def xml_text(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", value)


def write_reports(report: RunReport, directory: str | Path):
    folder = Path(directory)
    data = report.model_dump()
    data["summary"] = {
        "passed": sum(r.status == "passed" for r in report.results),
        "failed": sum(r.status == "failed" for r in report.results),
        "errors": sum(r.status == "error" for r in report.results),
        "exit_code": report.exit_code,
    }
    atomic_write(folder / "results.json", json.dumps(data, indent=2) + "\n")
    root = ET.Element(
        "testsuite",
        name=xml_text(report.suite),
        tests=str(len(report.results)),
        failures=str(data["summary"]["failed"]),
        errors=str(data["summary"]["errors"]),
        time=f"{report.elapsed_ms / 1000:.6f}",
    )
    properties = ET.SubElement(root, "properties")
    for name, value in {
        "run_id": report.run_id,
        "mode": report.mode,
        "expected": str(report.expected),
        "infrastructure_error": report.infrastructure_error or "",
    }.items():
        ET.SubElement(properties, "property", name=name, value=xml_text(value))
    for result in sorted(report.results, key=lambda r: r.case_id):
        case = ET.SubElement(
            root,
            "testcase",
            classname=xml_text(report.suite),
            name=result.case_id,
            time=f"{result.elapsed_ms / 1000:.6f}",
        )
        if result.status != "passed":
            messages = [
                f"{step.phase}/{step.name}: {'; '.join(step.messages)}"
                for step in result.steps
                if step.status != "passed"
            ]
            text = xml_text(result.message or "\n".join(messages) or result.status)
            element = ET.SubElement(
                case, "error" if result.status == "error" else "failure", message=text[:500]
            )
            element.text = text
    atomic_write(folder / "junit.xml", ET.tostring(root, encoding="unicode") + "\n")
