import io
import stat
import unittest
import zipfile

from mcp_packages import installation_settings, parse_package, within
from mcp_installation import sandbox_policy, sandbox_rejection, validate_result
from skill_packages import SkillError


def bundle(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


class McpPackageTests(unittest.TestCase):
    def test_program_without_skill_is_valid(self):
        content = bundle([("gm_cli/gm_cli.exe", b"binary"), ("gm_cli/install.ps1", b"script")])
        metadata, files = parse_package(content)
        self.assertEqual(metadata["fileCount"], 2)
        self.assertEqual(files["gm_cli/gm_cli.exe"], b"binary")
        self.assertEqual(parse_package(content)[0]["id"], metadata["id"])

    def test_multiple_nested_skills_are_data(self):
        metadata, _ = parse_package(bundle([("skills/a/SKILL.md", b"a"), ("skills/b/SKILL.md", b"b")]))
        self.assertEqual(metadata["fileCount"], 2)

    def test_unsafe_paths_and_collisions(self):
        for entries in ([('../outside', b'x')], [('C:/outside', b'x')],
                        [('a', b'x'), ('a/b', b'y')], [('A/x', b'x'), ('a/y', b'y')],
                        [('same', b'x'), ('SAME', b'y')], [('NUL.txt', b'x')], []):
            with self.subTest(entries=entries), self.assertRaises(SkillError):
                parse_package(bundle(entries))

    def test_symlink_rejected(self):
        info = zipfile.ZipInfo('link')
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(SkillError):
            parse_package(bundle([(info, b'target')]))

    def test_settings(self):
        self.assertFalse(installation_settings({})["enabled"])
        value = {"enabled": True, "temporaryRoot": r"C:\deploy\temp", "programRoot": r"C:\deploy\program"}
        self.assertTrue(installation_settings(value)["enabled"])
        for change in ({"platform": "macos"}, {"programRoot": r"C:\deploy\temp\child"},
                       {"temporaryRoot": "/tmp/mcp"}, {"enabled": "true"}, {"runtimes": "python"}):
            with self.subTest(change=change), self.assertRaises(SkillError):
                installation_settings({**value, **change})

    def test_single_installation_root_and_legacy_settings(self):
        rule = installation_settings({'enabled': True, 'installRoot': r'C:\deploy\mcp'})
        self.assertEqual(rule['temporaryRoot'], rule['programRoot'])
        self.assertEqual(installation_settings(rule), rule)
        with self.assertRaises(SkillError):
            installation_settings({'enabled': True, 'installRoot': ''})
        with self.assertRaises(SkillError):
            installation_settings({'enabled': True, 'installRoot': '../outside'})


class McpResultTests(unittest.TestCase):
    def setUp(self):
        self.root = r"C:\apps\demo"
        self.value = {"status": "installed", "name": "demo", "command": self.root + r"\demo.exe",
                      "args": ["mcp"], "cwd": self.root, "skillPath": None}

    def test_valid_executable_and_case_insensitive_boundary(self):
        self.assertEqual(validate_result(self.value, self.root), self.value)
        self.assertTrue(within(r"c:\APPS\demo\file", self.root))
        self.assertFalse(within(r"C:\apps\demo-other\file", self.root))

    def test_reject_outside_paths_and_reserved_names(self):
        for change in ({"name": "codex_orchestrator"}, {"name": "a.b"},
                       {"command": r"C:\Windows\cmd.exe"}, {"cwd": r"C:\other\dir"},
                       {"args": ["--token=secret"]}, {"skillPath": r"C:\skills\demo\SKILL.md"}):
            with self.subTest(change=change), self.assertRaises(SkillError):
                validate_result({**self.value, **change}, self.root)

    def test_runtime_requires_installed_script(self):
        runtime = r"C:\Python\python.exe"
        value = {**self.value, "command": runtime, "args": [self.root + r"\server.py"]}
        self.assertEqual(validate_result(value, self.root, [runtime])["command"], runtime)
        for args in (["-c", "code"], ["-m", "server"], [r"C:\elsewhere\server.py"]):
            with self.assertRaises(SkillError):
                validate_result({**value, "args": args}, self.root, [runtime])

    def test_existing_runtime_needs_no_manual_allowlist(self):
        for command in (r'C:\Python\python.exe', r'C:\Program Files\nodejs\node.exe'):
            value = {**self.value, 'command': command, 'args': [self.root + r'\server.py']}
            self.assertEqual(validate_result(value, self.root)['command'], command)
            with self.assertRaises(SkillError):
                validate_result({**value, 'args': ['-c', 'code']}, self.root)

    def test_sandbox_disables_network_and_implicit_temp_roots(self):
        policy = sandbox_policy(r"C:\temp\task", self.root)
        self.assertFalse(policy["networkAccess"])
        self.assertTrue(policy["excludeTmpdirEnvVar"])
        self.assertTrue(policy["excludeSlashTmp"])
        self.assertEqual(len(policy["writableRoots"]), 2)

    def test_sandbox_comparison_normalizes_windows_paths_but_not_permissions(self):
        policy = sandbox_policy(r'C:\deploy\temp', self.root)
        response = {'cwd': policy['writableRoots'][0], 'approvalPolicy': 'never', 'sandbox': {**policy,
            'writableRoots': list(reversed([p.upper() for p in policy['writableRoots']]))}}
        self.assertIsNone(sandbox_rejection(response, policy))
        for field, value in [('type', 'readOnly'), ('networkAccess', True),
                             ('excludeSlashTmp', False), ('writableRoots', [r'C:\other\root'])]:
            self.assertIsNotNone(sandbox_rejection({**response, 'sandbox': {**policy, field: value}}, policy))
        self.assertIn('只读', sandbox_rejection({'sandbox': {'type': 'readOnly'}}, policy))

    def test_implicit_cwd_is_counted_without_accepting_other_directories(self):
        policy = sandbox_policy(r'C:\deploy\temp', self.root)
        response = {'cwd': policy['writableRoots'][0], 'approvalPolicy': 'never',
                    'sandbox': {**policy, 'writableRoots': [self.root]}}
        self.assertIsNone(sandbox_rejection(response, policy))
        self.assertIsNotNone(sandbox_rejection({**response, 'cwd': r'C:\other\dir'}, policy))
        self.assertIsNotNone(sandbox_rejection({**response, 'sandbox': {**policy, 'writableRoots': []}}, policy))
        self.assertIsNotNone(sandbox_rejection({**response, 'sandbox': {**policy,
            'writableRoots': [self.root, r'C:\other\dir']}}, policy))

    def test_failed_result_discards_model_text(self):
        self.assertEqual(validate_result({**self.value, "status": "unsupported"}, self.root),
                         {"status": "unsupported"})


if __name__ == "__main__":
    unittest.main()
