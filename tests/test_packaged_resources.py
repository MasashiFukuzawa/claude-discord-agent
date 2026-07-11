from importlib import resources
from pathlib import Path


def test_packaged_resources_match_clone_compatibility_copies() -> None:
    root = Path(__file__).resolve().parent.parent
    packaged = resources.files("lib.resources")
    pairs = (
        (root / "worker-system-prompt.md", packaged.joinpath("worker-system-prompt.md")),
        (root / "specs/example-task.md", packaged.joinpath("specs/example-task.md")),
    )
    for clone_copy, packaged_copy in pairs:
        assert clone_copy.read_text(encoding="utf-8") == packaged_copy.read_text(
            encoding="utf-8"
        )
