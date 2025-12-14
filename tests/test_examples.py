"""Tests for examples - basic syntax and import checks."""

import ast
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def get_example_files():
    """Get all Python files in examples directory."""
    if not EXAMPLES_DIR.exists():
        return []
    return list(EXAMPLES_DIR.glob("*.py"))


@pytest.mark.parametrize(
    "example_file",
    get_example_files(),
    ids=lambda f: f.name,
)
def test_example_syntax(example_file: Path):
    """Test that example files have valid Python syntax."""
    source = example_file.read_text()
    # This will raise SyntaxError if invalid
    ast.parse(source)


@pytest.mark.parametrize(
    "example_file",
    get_example_files(),
    ids=lambda f: f.name,
)
def test_example_has_docstring(example_file: Path):
    """Test that example files have a module docstring."""
    source = example_file.read_text()
    tree = ast.parse(source)
    docstring = ast.get_docstring(tree)
    assert docstring is not None, f"{example_file.name} should have a docstring"


@pytest.mark.parametrize(
    "example_file",
    get_example_files(),
    ids=lambda f: f.name,
)
def test_example_has_main_guard(example_file: Path):
    """Test that example files have if __name__ == '__main__' guard."""
    source = example_file.read_text()
    assert (
        'if __name__ == "__main__"' in source or "if __name__ == '__main__'" in source
    ), f"{example_file.name} should have a main guard"
