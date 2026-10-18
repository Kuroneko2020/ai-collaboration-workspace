# -*- coding: utf-8 -*-
"""运行模板记忆工具的隔离基线；只把结果写入项目本地验证目录。"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ.pop("MEMORY_TEST_FAULT", None)

TEST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_DIR.parents[1]
LOG_DIR = PROJECT_ROOT / "本地" / "工具提取验证"


class Tee:
    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cases: list[dict[str, str]] = []

    def addSuccess(self, test) -> None:
        super().addSuccess(test)
        self.cases.append({"test": test.id(), "status": "passed"})

    def addSkip(self, test, reason: str) -> None:
        super().addSkip(test, reason)
        self.cases.append({"test": test.id(), "status": "skipped", "reason": reason})

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self.cases.append({"test": test.id(), "status": "failed"})

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self.cases.append({"test": test.id(), "status": "error"})


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    log_path = LOG_DIR / f"baseline-{run_id}.log"
    result_path = LOG_DIR / f"baseline-{run_id}.json"
    sys.path.insert(0, str(TEST_DIR))
    command = "py -3 -B -X utf8 ./测试/记忆工具/run_baseline.py"
    with log_path.open("x", encoding="utf-8", newline="\n") as log:
        stream = Tee(sys.stdout, log)
        stream.write(f"Command: {command}\n")
        stream.write(f"Started (UTC): {started.isoformat()}\n")
        stream.write(f"Python: {platform.python_version()} / {sys.platform}\n")
        stream.write("Isolation: all functional CLI calls use temporary --root; source bytecode disabled.\n\n")
        suite = unittest.defaultTestLoader.discover(str(TEST_DIR), pattern="test_*.py")
        count = suite.countTestCases()
        timer = time.monotonic()
        result = unittest.TextTestRunner(
            stream=stream, verbosity=2, resultclass=RecordingResult
        ).run(suite)
        elapsed = time.monotonic() - timer
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "bytecode_disabled": True,
            "isolation": "tempfile.mkdtemp roots; no production/template runtime commands",
            "tests_discovered": count,
            "tests_run": result.testsRun,
            "passed": sum(case["status"] == "passed" for case in result.cases),
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "successful": result.wasSuccessful(),
            "elapsed_seconds": round(elapsed, 3),
            "cases": result.cases,
            "log_project_relative": log_path.relative_to(PROJECT_ROOT).as_posix(),
        }
        with result_path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        stream.write(f"\nResult: {result_path.relative_to(PROJECT_ROOT).as_posix()}\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
