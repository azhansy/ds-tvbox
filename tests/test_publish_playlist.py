import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_playlist as publish


class PublishChecks(unittest.TestCase):
    def test_upload_verify_replace_and_failures_preserve_old(self):
        for scenario in ('success', 'unchanged', 'upload_failure', 'bad_download', 'rename_failure', 'recover'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'result.m3u'
                path.write_bytes(b'new playlist')
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                assets = [{ 'id': 1, 'name': 'previous-1.m3u' if scenario == 'recover' else 'result.m3u'}]
                content = {1: b'new playlist' if scenario == 'unchanged' else b'old playlist'}
                events = []
                def api(endpoint, **fields):
                    if endpoint.endswith('/releases/10'):
                        return {'assets': assets}
                    asset_id = int(endpoint.rsplit('/', 1)[1])
                    events.append(('rename', asset_id, fields['name']))
                    if asset_id == 2 and scenario == 'rename_failure':
                        raise RuntimeError('rename failed')
                    assets[asset_id - 1]['name'] = fields['name']
                    return assets[asset_id - 1]
                def gh(*args):
                    if args[:2] == ('release', 'upload'):
                        events.append(('upload',))
                        if scenario == 'upload_failure':
                            raise RuntimeError('upload failed')
                        file = Path(args[3])
                        assets.append({'id': 2, 'name': file.name})
                        content[2] = file.read_bytes()
                        return b''
                    asset_id = int(args[1].rsplit('/', 1)[1])
                    events.append(('download', asset_id))
                    return b'corrupt' if asset_id == 2 and scenario == 'bad_download' else content[asset_id]
                with patch.object(publish, 'api', side_effect=api), patch.object(publish, 'gh', side_effect=gh):
                    if scenario in ('upload_failure', 'bad_download', 'rename_failure'):
                        with self.assertRaises((ValueError, RuntimeError)):
                            publish.replace_asset('repos/test/repo', {'id': 10, 'assets': assets}, path, digest)
                        self.assertEqual(assets[0]['name'], 'result.m3u')
                        self.assertEqual(content[1], b'old playlist')
                    else:
                        changed = publish.replace_asset('repos/test/repo', {'id': 10, 'assets': assets}, path, digest)
                        self.assertEqual(changed, scenario != 'unchanged')
                        stable = next(a for a in assets if a['name'] == 'result.m3u')
                        self.assertEqual(content[stable['id']], path.read_bytes())
                        if changed:
                            self.assertLess(events.index(('download', 2)), events.index(('rename', 1, 'previous-1.m3u')))
                        else:
                            self.assertNotIn(('upload',), events)

    def test_unchecked_file_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            (root / 'build/result.m3u').write_bytes(b'wrong')
            (root / 'checks.json').write_text(json.dumps({'live': {'status': 'ok', 'version': 'different'}}))
            with patch.object(publish, 'ROOT', root), patch.dict(publish.os.environ, {'GITHUB_REPOSITORY': 'test/repo'}), \
                    patch.object(publish, 'gh') as gh:
                with self.assertRaises(ValueError):
                    publish.main()
                gh.assert_not_called()

    def test_two_day_schedule_across_month_boundary(self):
        from datetime import datetime, timedelta, timezone
        workflow = (publish.ROOT / '.github/workflows/refresh.yml').read_text()
        gate = workflow.split('        run: |\n', 1)[1].split('      - uses:', 1)[0]
        gate = '\n'.join(line[10:] for line in gate.splitlines())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            due = []
            start = datetime(2026, 1, 29, 19, 17, tzinfo=timezone.utc)
            for day in range(8):
                timestamp = int((start + timedelta(days=day)).timestamp())
                output.write_text('')
                env = dict(publish.os.environ, EVENT_NAME='schedule', GITHUB_OUTPUT=str(output))
                subprocess.run(['bash', '-c', f'date() {{ echo {timestamp}; }}\n' + gate], env=env, check=True)
                if 'due=true' in output.read_text():
                    due.append(day)
            self.assertEqual([b - a for a, b in zip(due, due[1:])], [2, 2, 2])
            output.write_text('')
            env['EVENT_NAME'] = 'workflow_dispatch'
            subprocess.run(['bash', '-c', gate], env=env, check=True)
            self.assertIn('due=true', output.read_text())


if __name__ == '__main__':
    unittest.main()
