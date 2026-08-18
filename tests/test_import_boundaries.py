import os
from pathlib import Path
import subprocess
import sys


def test_eval_suite_dependencies_import_in_fresh_interpreter():
    repo_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    python_path = str(repo_root / "src")
    if environment.get("PYTHONPATH"):
        python_path = f"{python_path}{os.pathsep}{environment['PYTHONPATH']}"
    environment["PYTHONPATH"] = python_path

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cogdoc.tools.eval.quality_metrics",
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
