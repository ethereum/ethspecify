import contextlib
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

from ethspecify import cli, core


class CliTagPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the repository's real, tagged spec data; only HTTP delivery is
        # replaced. Spec lookup, hashing, replacement and the CLI all run.
        root = pathlib.Path(__file__).resolve().parents[1]
        cls.snapshot = (root / 'pyspec/v1.6.0/pyspec.json').read_bytes()
        cls.links = (root / 'pyspec/v1.6.0/links.json').read_bytes()
        cls.spec = json.loads(cls.snapshot)['mainnet']['phase0']['functions']['is_active_validator']
        cls.digest = hashlib.sha256(cls.spec.encode('utf-8')).hexdigest()[:8]
        cls.tag = '<spec fn="is_active_validator" fork="phase0" style="full"'

    def run_cli(self, directory):
        def response(url):
            responses = {
                'https://raw.githubusercontent.com/ethereum/ethspecify/main/pyspec/v1.6.0/pyspec.json': self.snapshot,
                'https://raw.githubusercontent.com/ethereum/ethspecify/main/pyspec/v1.6.0/links.json': self.links,
            }
            self.assertIn(url, responses)
            result = requests.Response()
            result.status_code = 200
            result._content = responses[url]
            return result

        core.get_pyspec.cache_clear()
        core.get_links.cache_clear()
        with patch.object(sys, 'argv', ['ethspecify', 'process', '--path', str(directory)]), \
                patch.object(core.requests, 'get', side_effect=response), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exited:
                cli.main()
            self.assertEqual(exited.exception.code, 0)
        core.get_pyspec.cache_clear()
        core.get_links.cache_clear()

    def check_expansion(self, before, prefix='', preceding=''):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            (directory / '.ethspecify.yml').write_text('version: v1.6.0\n', encoding='utf-8')
            source = directory / 'client.txt'
            source.write_text(before, encoding='utf-8')
            self.run_cli(directory)
            body = '\n'.join(prefix + line if line.rstrip() else prefix.rstrip()
                             for line in self.spec.split('\n'))
            expected = '{}{}{} hash="{}">\n{}\n{}</spec>\n'.format(
                preceding, prefix, self.tag, self.digest, body, prefix)
            self.assertEqual(source.read_text(encoding='utf-8'), expected)
            self.run_cli(directory)
            self.assertEqual(source.read_text(encoding='utf-8'), expected, 'expansion must be idempotent')

    def test_self_closing_full_tag_at_start_of_file(self):
        self.check_expansion(self.tag + ' />\n')

    def test_paired_full_tag_at_start_of_file(self):
        self.check_expansion(self.tag + '>\noutdated spec\n</spec>\n')

    def test_column_zero_tag_does_not_copy_previous_line(self):
        preceding = '# Client implementation references\n'
        self.check_expansion(preceding + self.tag + ' />\n', preceding=preceding)

    def test_comment_prefix_is_preserved(self):
        prefix = '// '
        self.check_expansion(prefix + self.tag + ' />\n', prefix=prefix)

    def test_indented_tag_uses_only_its_own_line_prefix(self):
        preceding, prefix = '# Previous line\n', '    '
        self.check_expansion(preceding + prefix + self.tag + ' />\n', prefix, preceding)

    def test_hash_style_at_start_of_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            (directory / '.ethspecify.yml').write_text('version: v1.6.0\n', encoding='utf-8')
            source = directory / 'client.txt'
            source.write_text('<spec fn="is_active_validator" fork="phase0" />\n', encoding='utf-8')
            self.run_cli(directory)
            self.assertEqual(source.read_text(encoding='utf-8'),
                '<spec fn="is_active_validator" fork="phase0" hash="{}" />\n'.format(self.digest))

    def check_other_style(self, tag, expected_body):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            (directory / '.ethspecify.yml').write_text('version: v1.6.0\n', encoding='utf-8')
            source = directory / 'client.txt'
            source.write_text(tag + ' />\n', encoding='utf-8')
            self.run_cli(directory)
            expanded = source.read_text(encoding='utf-8')
            self.assertTrue(expanded.startswith(tag + ' hash="'))
            self.assertIn(expected_body, expanded)
            self.assertTrue(expanded.endswith('\n</spec>\n'))
            self.run_cli(directory)
            self.assertEqual(source.read_text(encoding='utf-8'), expanded)

    def test_link_style_at_start_of_file(self):
        tag = '<spec fn="is_active_validator" fork="phase0" style="link"'
        self.check_other_style(tag,
            '\nhttps://github.com/ethereum/consensus-specs/blob/v1.6.0/specs/phase0/beacon-chain.md?plain=1#L710-L714\n')

    def test_diff_style_at_start_of_file(self):
        tag = '<spec fn="is_eligible_for_activation_queue" fork="electra" style="diff"'
        self.check_other_style(tag, '\n--- phase0\n+++ electra\n')


if __name__ == '__main__':
    unittest.main()
