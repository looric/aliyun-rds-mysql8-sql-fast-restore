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


class RobustnessTests(unittest.TestCase):
    def make_runtime_args(self, **overrides):
        values = dict(
            dry_run=False,
            defaults_extra_file=None,
            auth_method="defaults-extra-file",
            ask_password=False,
            password_file=None,
            db_pass=None,
            login_path=None,
            mysql="mysql",
            db_host="127.0.0.1",
            db_port="3306",
            db_user="root",
            default_character_set="utf8mb4",
            max_allowed_packet="1G",
            force=False,
            compression_algorithms=None,
            mysql_extra_args=[],
            disable_binlog=False,
            no_transaction=False,
            keep_manifests_dir=None,
            generated_column_mode="default",
            input_mode="source",
            chunk_size=restore.DEFAULT_CHUNK_SIZE,
            process_cleanup_timeout=0.01,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_common_session_init_is_shared_by_source_and_stream(self):
        args = self.make_runtime_args(disable_binlog=True, no_transaction=False)
        sql = restore.common_session_init_sql(args, data_mode=True)
        self.assertIn("SET SESSION sql_log_bin=0;", sql)
        self.assertIn("SET SESSION foreign_key_checks=0;", sql)
        self.assertIn("SET SESSION unique_checks=0;", sql)
        self.assertIn("SET SESSION autocommit=0;", sql)
        self.assertNotIn("autocommit", restore.common_session_init_sql(args, data_mode=False))

    def test_source_data_manifest_contains_bulk_load_session_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "manifests"
            keep.mkdir()
            data = root / "t1_0_part0.sql"
            data.write_text("INSERT INTO `db1`.`t1` VALUES (1);\n", encoding="utf-8")
            table = restore.TablePlan("db1", "t1", root, root / "structure.sql", [data])
            args = self.make_runtime_args(keep_manifests_dir=str(keep), disable_binlog=True)
            auth = restore.AuthManager(args)
            captured = {}
            old = restore.run_mysql_from_manifest

            def fake_run(call_args, call_auth, manifest_path, description, data_mode):
                captured["description"] = description
                captured["data_mode"] = data_mode
                captured["text"] = Path(manifest_path).read_text(encoding="utf-8")

            restore.run_mysql_from_manifest = fake_run
            try:
                restore.import_table_data_source(args, auth, table)
            finally:
                restore.run_mysql_from_manifest = old
            self.assertEqual(captured["description"], "data db1.t1")
            self.assertTrue(captured["data_mode"])
            self.assertIn("SET SESSION sql_log_bin=0;", captured["text"])
            self.assertIn("SET SESSION foreign_key_checks=0;", captured["text"])
            self.assertIn("SET SESSION unique_checks=0;", captured["text"])
            self.assertIn("SET SESSION autocommit=0;", captured["text"])
            self.assertIn("source ", captured["text"])

    def test_write_bytes_reports_broken_pipe(self):
        class BadStdin(object):
            def write(self, data):
                raise BrokenPipeError("closed")

        class FakeProc(object):
            stdin = BadStdin()
            def poll(self):
                return 1

        with self.assertRaises(RuntimeError) as ctx:
            restore.write_bytes(FakeProc(), b"SELECT 1")
        self.assertIn("closed stdin", str(ctx.exception))

    def test_close_stdin_ignores_broken_pipe(self):
        class BadStdin(object):
            def close(self):
                raise BrokenPipeError("closed")

        class FakeProc(object):
            stdin = BadStdin()

        restore.close_stdin(FakeProc())

    def test_flush_stdin_reports_broken_pipe(self):
        class BadStdin(object):
            def flush(self):
                raise BrokenPipeError("closed")

        class FakeProc(object):
            stdin = BadStdin()
            def poll(self):
                return 2

        with self.assertRaises(RuntimeError) as ctx:
            restore.flush_stdin(FakeProc(), "test import")
        self.assertIn("flushing input", str(ctx.exception))

    def test_auth_manager_exit_does_not_mask_restore_exception(self):
        args = self.make_runtime_args()
        auth = restore.AuthManager(args)
        old = restore.cleanup_temp_files

        def bad_cleanup(paths):
            raise OSError("cleanup failed")

        restore.cleanup_temp_files = bad_cleanup
        try:
            self.assertFalse(auth.__exit__(RuntimeError, RuntimeError("restore failed"), None))
        finally:
            restore.cleanup_temp_files = old

    def test_sql_parser_helpers_ignore_quoted_text_and_comments(self):
        text = "INSERT INTO `t` (`id`, `values_col`) VALUES (1, 'not VALUES'), (2, 'x,y');"
        pos = restore.find_keyword_outside(text, "VALUES", 0)
        self.assertGreater(pos, text.index("`values_col`"))
        self.assertEqual(text[pos:pos + len("VALUES")].upper(), "VALUES")
        parts = restore.split_top_level_commas("`a`, concat('x,y', func(1,2)), /* c,d */ `b`")
        self.assertEqual([part.strip() for part in parts], ["`a`", "concat('x,y', func(1,2))", "/* c,d */ `b`"])
        ident, end = restore.parse_backtick_identifier("`a``b` tail")
        self.assertEqual(ident, "a`b")
        self.assertEqual(end, len("`a``b`"))


if __name__ == "__main__":
    unittest.main()
