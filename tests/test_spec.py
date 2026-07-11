"""lib/spec.py のテスト: 正常系・不正 spec・required_vars 不足・補間・list_specs。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.spec import SpecError, _parse_spec_file, list_specs, load_spec

VALID_SPEC = (
    "---toml\n"
    'id = "test-spec"\n'
    'description = "テスト用spec"\n'
    'required_vars = ["branch", "task"]\n'
    'optional_vars = ["extra"]\n'
    "---\n"
    "ブランチ ${branch} で ${task} を実行する。\n"
    "追加情報: ${extra}\n"
)

MINIMAL_SPEC = (
    "---toml\n"
    'id = "minimal"\n'
    'description = "最小spec"\n'
    "required_vars = []\n"
    "optional_vars = []\n"
    "---\n"
    "固定タスク: テストを実行してください。\n"
)


def _write_spec(tmpdir: Path, filename: str, content: str) -> Path:
    p = tmpdir / filename
    p.write_text(content, encoding="utf-8")
    return p


class TestSpecParse(unittest.TestCase):
    """_parse_spec_file の正常系テスト。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def test_parse_valid_spec(self):
        p = _write_spec(self.tmpdir, "test.md", VALID_SPEC)
        spec = _parse_spec_file(p)
        self.assertEqual(spec.id, "test-spec")
        self.assertEqual(spec.description, "テスト用spec")
        self.assertEqual(spec.required_vars, ["branch", "task"])
        self.assertEqual(spec.optional_vars, ["extra"])
        self.assertIn("${branch}", spec.task_template)

    def test_parse_minimal_spec(self):
        p = _write_spec(self.tmpdir, "minimal.md", MINIMAL_SPEC)
        spec = _parse_spec_file(p)
        self.assertEqual(spec.id, "minimal")
        self.assertEqual(spec.required_vars, [])
        self.assertIn("固定タスク", spec.task_template)


class TestSpecValidation(unittest.TestCase):
    """Spec.validate() / parse エラーケーステスト。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def test_missing_frontmatter(self):
        p = _write_spec(self.tmpdir, "no_fm.md", "frontmatterなし\nただのMarkdown")
        with self.assertRaises(SpecError) as ctx:
            _parse_spec_file(p)
        self.assertIn("frontmatter", str(ctx.exception))

    def test_invalid_toml(self):
        bad = "---toml\nid = [unclosed\n---\nbody"
        p = _write_spec(self.tmpdir, "bad_toml.md", bad)
        with self.assertRaises(SpecError) as ctx:
            _parse_spec_file(p)
        self.assertIn("TOML", str(ctx.exception))

    def test_missing_id(self):
        no_id = '---toml\ndescription = "desc"\nrequired_vars = []\noptional_vars = []\n---\nbody\n'
        p = _write_spec(self.tmpdir, "no_id.md", no_id)
        with self.assertRaises(SpecError) as ctx:
            _parse_spec_file(p)
        self.assertIn("id", str(ctx.exception))

    def test_missing_description(self):
        no_desc = '---toml\nid = "x"\nrequired_vars = []\noptional_vars = []\n---\nbody\n'
        p = _write_spec(self.tmpdir, "no_desc.md", no_desc)
        with self.assertRaises(SpecError) as ctx:
            _parse_spec_file(p)
        self.assertIn("description", str(ctx.exception))

    def test_empty_body(self):
        empty_body = (
            '---toml\nid = "x"\ndescription = "d"\nrequired_vars = []\noptional_vars = []\n---\n'
        )
        p = _write_spec(self.tmpdir, "empty_body.md", empty_body)
        with self.assertRaises(SpecError) as ctx:
            _parse_spec_file(p)
        self.assertIn("task_template", str(ctx.exception))


class TestSpecRender(unittest.TestCase):
    """Spec.render() のテスト。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        p = _write_spec(self.tmpdir, "test.md", VALID_SPEC)
        self.spec = _parse_spec_file(p)

    def test_render_all_vars(self):
        result = self.spec.render({"branch": "feat/x", "task": "lint", "extra": "追加情報"})
        self.assertIn("feat/x", result)
        self.assertIn("lint", result)
        self.assertIn("追加情報", result)
        self.assertNotIn("${", result)

    def test_render_optional_defaults_to_empty(self):
        result = self.spec.render({"branch": "main", "task": "test"})
        self.assertIn("main", result)
        self.assertNotIn("${extra}", result)

    def test_render_missing_required_vars(self):
        with self.assertRaises(SpecError) as ctx:
            self.spec.render({"branch": "main"})
        self.assertIn("task", str(ctx.exception))
        self.assertIn("Missing", str(ctx.exception))

    def test_render_unknown_var_in_template(self):
        bad_tmpl = (
            "---toml\n"
            'id = "x"\n'
            'description = "d"\n'
            "required_vars = []\n"
            "optional_vars = []\n"
            "---\n"
            "${unknown} を使う\n"
        )
        tmpdir2 = Path(tempfile.mkdtemp())
        p = _write_spec(tmpdir2, "bad_tmpl.md", bad_tmpl)
        spec = _parse_spec_file(p)
        with self.assertRaises(SpecError) as ctx:
            spec.render({})
        self.assertIn("unknown", str(ctx.exception))


class TestLoadSpec(unittest.TestCase):
    """load_spec() の検索・ロードテスト。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        _write_spec(
            self.tmpdir,
            "my-spec.md",
            VALID_SPEC.replace("test-spec", "my-spec").replace("テスト用spec", "my spec"),
        )

    def test_load_by_id(self):
        spec = load_spec("my-spec", search_dirs=[self.tmpdir])
        self.assertEqual(spec.id, "my-spec")

    def test_load_not_found(self):
        with self.assertRaises(SpecError) as ctx:
            load_spec("nonexistent", search_dirs=[self.tmpdir])
        self.assertIn("not found", str(ctx.exception))

    def test_load_by_absolute_path(self):
        p = self.tmpdir / "my-spec.md"
        spec = load_spec(str(p))
        self.assertEqual(spec.id, "my-spec")


class TestListSpecs(unittest.TestCase):
    """list_specs() のテスト。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        _write_spec(
            self.tmpdir,
            "spec-a.md",
            VALID_SPEC.replace("test-spec", "spec-a").replace("テスト用spec", "spec A"),
        )
        _write_spec(
            self.tmpdir,
            "spec-b.md",
            MINIMAL_SPEC.replace("minimal", "spec-b").replace("最小spec", "spec B"),
        )
        _write_spec(self.tmpdir, "broken.md", "frontmatterなし")

    def test_lists_valid_specs(self):
        specs = list_specs(search_dirs=[self.tmpdir])
        ids = [s["id"] for s in specs]
        self.assertIn("spec-a", ids)
        self.assertIn("spec-b", ids)

    def test_skips_broken_spec(self):
        specs = list_specs(search_dirs=[self.tmpdir])
        ids = [s["id"] for s in specs]
        self.assertNotIn("broken", ids)

    def test_empty_dir(self):
        empty = Path(tempfile.mkdtemp())
        specs = list_specs(search_dirs=[empty])
        self.assertEqual(specs, [])

    def test_nonexistent_dir(self):
        specs = list_specs(search_dirs=[Path("/nonexistent/path")])
        self.assertEqual(specs, [])


class TestSampleSpecs(unittest.TestCase):
    """実際の specs/ ディレクトリのサンプル spec をロード・レンダリングできるか確認。"""

    SPECS_DIR = Path(__file__).parent.parent / "specs"

    def test_example_task_load(self):
        spec = load_spec("example-task", search_dirs=[self.SPECS_DIR])
        self.assertEqual(spec.id, "example-task")
        self.assertIn("task_description", spec.required_vars)

    def test_example_task_render(self):
        spec = load_spec("example-task", search_dirs=[self.SPECS_DIR])
        rendered = spec.render(
            {
                "task_description": "Implement the requested validation",
                "verification": "Run the repository test suite",
            }
        )
        self.assertIn("Implement the requested validation", rendered)
        self.assertIn("Run the repository test suite", rendered)
        self.assertIn("<<<DISCORD_AGENT_RESULT>>>", rendered)
        self.assertNotIn("${task_description}", rendered)


if __name__ == "__main__":
    unittest.main()
