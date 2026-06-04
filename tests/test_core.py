import argparse
import contextlib
import io
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "restore_sql_fast.py"

spec = importlib.util.spec_from_file_location("restore_sql_fast", str(SCRIPT))
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class CoreTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = dict(
            database=None,
            allow_no_database=False,
            ignore_non_sql=False,
            strict_layout=False,
            layout="auto",
            only_database=None,
            detect_limit=restore.DEFAULT_DETECT_DB_READ_LIMIT,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_detect_utf8_database_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "structure.sql"
            path.write_text("-- 中文注释\nCREATE DATABASE `服务db`;\nUSE `服务db`;\n", encoding="utf-8")
            self.assertEqual(restore.detect_database_from_structure(path, 0), "服务db")

    def test_rewrite_create_database_preserves_utf8_payload(self):
        original = "-- 中文注释\nCREATE DATABASE `服务db`;\n".encode("utf-8")
        rewritten, count = restore.rewrite_create_database_bytes(original)
        self.assertEqual(count, 1)
        self.assertEqual(rewritten.decode("utf-8"), "-- 中文注释\nCREATE DATABASE IF NOT EXISTS `服务db`;\n")

    def test_option_file_password_allows_comment_chars_inside_quotes(self):
        self.assertEqual(restore.mysql_option_file_value("abc#def"), '"abc#def"')
        self.assertEqual(restore.mysql_option_file_value("abc;def"), '"abc;def"')
        self.assertEqual(restore.mysql_option_file_value('abc"def'), '"abc\\"def"')
        with self.assertRaises(RuntimeError):
            restore.mysql_option_file_value("abc\ndef")
        self.assertEqual(restore.normalize_password_for_option_file("abc\x00\x00", "test"), "abc")
        with self.assertRaises(RuntimeError):
            restore.normalize_password_for_option_file("abc\x00def", "test")

    def test_sample_password_option_file_value(self):
        password = "HVNRSVvPskwJLApCXFW3UpdlZ5oiXoA1mC7jOAoQ5urJuC9JoQWlhqt2"
        self.assertEqual(restore.mysql_option_file_value(password), '"{}"'.format(password))

    def test_mysql_pwd_is_not_used(self):
        old = os.environ.get("MYSQL_PWD")
        os.environ["MYSQL_PWD"] = "should_not_leak"
        try:
            args = argparse.Namespace(
                dry_run=True,
                defaults_extra_file=None,
                auth_method="defaults-extra-file",
                ask_password=False,
                password_file=None,
                db_pass=None,
                login_path="restore",
            )
            with restore.AuthManager(args) as auth:
                self.assertNotIn("MYSQL_PWD", auth.child_env())
        finally:
            if old is None:
                os.environ.pop("MYSQL_PWD", None)
            else:
                os.environ["MYSQL_PWD"] = old

    def test_dash_password_rejected(self):
        args = argparse.Namespace(
            defaults_extra_file=None,
            auth_method="defaults-extra-file",
            ask_password=False,
            password_file=None,
            db_pass="-",
            login_path=None,
        )
        with self.assertRaises(RuntimeError):
            restore.resolve_password(args)

    def test_auth_method_env_is_not_available(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                restore.parse_args(["/tmp/data", "127.0.0.1", "3306", "root", "--auth-method", "env"])

    def test_source_safe_path_rejects_unsafe_chars(self):
        with self.assertRaises(RuntimeError):
            restore.require_source_safe_path(Path("/tmp/restore data/file.sql"))
        with self.assertRaises(RuntimeError):
            restore.require_source_safe_path(Path("/tmp/restore;data/file.sql"))
        restore.require_source_safe_path(Path("/tmp/restore-data/file_1.sql"))

    def test_build_flat_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "structure.sql").write_text("CREATE DATABASE `db1`;\nUSE `db1`;\n", encoding="utf-8")
            table = root / "t1"
            (table / "data").mkdir(parents=True)
            (table / "structure.sql").write_text("CREATE TABLE `t1` (`id` int);\n", encoding="utf-8")
            (table / "data" / "t1_0_part0.sql").write_text("INSERT INTO `t1` VALUES (1);\n", encoding="utf-8")
            plans = restore.build_plan(root, self.make_args())
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].layout, "flat")
            self.assertEqual(plans[0].db_name, "db1")
            self.assertEqual(plans[0].tables[0].full_name(), "db1.t1")



    def test_detect_generated_columns_from_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "structure.sql"
            path.write_text(
                "CREATE TABLE `metabase_field` (\n"
                "  `id` int NOT NULL,\n"
                "  `comment_col` varchar(20) COMMENT 'AS (not generated)',\n"
                "  `unique_field_helper` int GENERATED ALWAYS AS (`id` + 1) STORED,\n"
                "  PRIMARY KEY (`id`)\n"
                ");\n",
                encoding="utf-8",
            )
            self.assertEqual(restore.detect_generated_columns_from_structure(path), ["unique_field_helper"])

    def test_rewrite_generated_column_values_to_default(self):
        statement = (
            "INSERT IGNORE INTO `service2024report`.`metabase_field` "
            "(`id`, `name`, `unique_field_helper`) VALUES "
            "(1, '中文', 2),(2, 'x,y', 3);"
        )
        rewritten, rows = restore.rewrite_insert_generated_defaults(statement, {"unique_field_helper"})
        self.assertEqual(rows, 2)
        self.assertIn("(1, '中文', DEFAULT)", rewritten)
        self.assertIn("(2, 'x,y', DEFAULT)", rewritten)

    def test_build_nested_plan_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for db in ["db1", "db2"]:
                db_dir = root / db
                table = db_dir / "t1"
                (table / "data").mkdir(parents=True)
                (db_dir / "structure.sql").write_text("CREATE DATABASE `{}`;\n".format(db), encoding="utf-8")
                (table / "structure.sql").write_text("CREATE TABLE `t1` (`id` int);\n", encoding="utf-8")
                (table / "data" / "t1_0_part0.sql").write_text("INSERT INTO `t1` VALUES (1);\n", encoding="utf-8")
            plans = restore.build_plan(root, self.make_args(only_database={"db2"}))
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].layout, "nested")
            self.assertEqual(plans[0].db_name, "db2")


if __name__ == "__main__":
    unittest.main()
