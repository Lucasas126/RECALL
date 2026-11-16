"""The entry points must load .env before importing modules whose settings read environment variables."""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class EntrypointEnvTests(unittest.TestCase):
    def _first_src_import_line(self, tree: ast.Module) -> int:
        lines = [
            node.lineno for node in tree.body
            if (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src."))
        ]
        return min(lines)

    def test_dotenv_is_loaded_before_src_imports(self) -> None:
        for name in ("web.py", "run_telegram.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            load = next(
                node.lineno for node in tree.body
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "attr", "") == "load_dotenv"
            )
            self.assertLess(load, self._first_src_import_line(tree), name)


if __name__ == "__main__":
    unittest.main()
