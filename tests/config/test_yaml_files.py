import os
import tempfile
import unittest
from pathlib import Path

import yaml

from src.config import yaml_files
from src.config.yaml_files import clear_yaml_cache, load_yaml_file, parse_yaml


class YamlFilesTest(unittest.TestCase):
    def setUp(self):
        clear_yaml_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agent.yaml"

    def tearDown(self):
        self.tmp.cleanup()
        clear_yaml_cache()

    def test_parse_matches_safe_load(self):
        text = "id: nova\nlist: [1, 2]\nnested:\n  key: 'value'\nempty:\n"
        self.assertEqual(parse_yaml(text), yaml.safe_load(text))

    def test_parse_rejects_python_tags_like_safe_load(self):
        with self.assertRaises(yaml.YAMLError):
            parse_yaml("!!python/object/apply:os.system ['true']")

    def test_unchanged_file_is_parsed_once(self):
        self.path.write_text("id: nova\n", encoding="utf-8")
        calls = []
        real = yaml_files.parse_yaml
        yaml_files.parse_yaml = lambda text: calls.append(text) or real(text)
        try:
            self.assertEqual(load_yaml_file(self.path), {"id": "nova"})
            self.assertEqual(load_yaml_file(self.path), {"id": "nova"})
        finally:
            yaml_files.parse_yaml = real
        self.assertEqual(len(calls), 1)

    def test_a_changed_file_is_read_again(self):
        self.path.write_text("id: nova\n", encoding="utf-8")
        self.assertEqual(load_yaml_file(self.path), {"id": "nova"})
        info = self.path.stat()
        self.path.write_text("id: sage\n", encoding="utf-8")  # same size, maybe the same clock tick
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
        replaced = Path(self.tmp.name) / "new.yaml"
        replaced.write_text("id: sage\n", encoding="utf-8")
        os.replace(replaced, self.path)  # an atomic write: a new inode
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(load_yaml_file(self.path), {"id": "sage"})

    def test_callers_get_a_copy(self):
        self.path.write_text("tools: [a, b]\n", encoding="utf-8")
        first = load_yaml_file(self.path)
        first["tools"].append("c")
        self.assertEqual(load_yaml_file(self.path), {"tools": ["a", "b"]})

    def test_errors_are_raised_and_not_cached(self):
        self.path.write_text("id: [unclosed\n", encoding="utf-8")
        with self.assertRaises(yaml.YAMLError):
            load_yaml_file(self.path)
        self.path.write_text("id: fixed\n", encoding="utf-8")
        self.assertEqual(load_yaml_file(self.path), {"id": "fixed"})
        with self.assertRaises(FileNotFoundError):
            load_yaml_file(Path(self.tmp.name) / "missing.yaml")


if __name__ == "__main__":
    unittest.main()
