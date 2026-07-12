from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "windows_install.bat"


def _commands():
    return [
        line.strip()
        for line in INSTALL_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().upper().startswith("REM ")
    ]


@pytest.mark.parametrize(
    ("command", "label"),
    [
        pytest.param('cd /d "%ROOT_DIR%"', "project-directory", id="cd"),
        pytest.param("pip install poetry", "poetry-install", id="pip"),
        pytest.param("poetry install", "dependency-install", id="poetry"),
        pytest.param(
            'copy "%ROOT_DIR%src\\chanlun\\config.py.demo" '
            '"%ROOT_DIR%src\\chanlun\\config.py" >nul',
            "config-copy",
            id="copy",
        ),
        pytest.param(
            'poetry run python "%ROOT_DIR%check_env.py"',
            "environment-check",
            id="check-env",
        ),
    ],
)
def test_critical_command_is_followed_by_failure_guard(command, label):
    commands = _commands()
    index = commands.index(command)

    assert commands[index + 1].lower() == "if errorlevel 1 goto :fail", label


def test_missing_environment_checker_is_fatal():
    source = INSTALL_SCRIPT.read_text(encoding="utf-8").lower()
    marker = 'if not exist "%root_dir%check_env.py" ('
    start = source.index(marker)
    block = source[start : source.index(")", start) + 1]

    assert "goto :fail" in block


def test_batch_has_explicit_success_and_failure_exits():
    commands = [line.lower() for line in _commands()]

    assert "exit /b 0" in commands
    assert ":fail" in commands
    assert "exit /b 1" in commands
