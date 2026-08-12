from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "tools" / "build_stable_gui_launcher.ps1"


def _build(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "launcher"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-OutputDirectory",
            str(output),
            "-TargetRoot",
            str(ROOT),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output / "AutoResonance.exe", output / "launcher-target.txt"


def test_stable_launcher_builds_and_validates_without_starting_gui(tmp_path):
    executable, target_file = _build(tmp_path)

    assert executable.is_file()
    assert target_file.read_text(encoding="utf-8").strip() == str(ROOT)
    validation = subprocess.run(
        [str(executable), "--validate-only"],
        check=False,
        timeout=10,
    )
    assert validation.returncode == 0


def test_stable_launcher_rejects_missing_target_without_starting_gui(tmp_path):
    executable, target_file = _build(tmp_path)
    target_file.write_text(str(tmp_path / "missing"), encoding="utf-8")

    validation = subprocess.run(
        [str(executable), "--validate-only"],
        check=False,
        timeout=10,
    )
    assert validation.returncode == 2
