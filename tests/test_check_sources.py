import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import check_sources as check


class Checks(unittest.TestCase):
    def test_config_wrapper_and_invalid_responses(self):
        config = b'{/* comment */"sites":[{"key":"a","name":"A","type":3,"api":"csp_A"}],}'
        self.assertEqual(check.decode_config(config), check.decode_config(b'image**' + base64.b64encode(config)))
        for body in (b'<html>200 OK</html>', b'{}', b'{"sites":[]}', b'image**a@@',
                     b'{"sites":[{"key":"a"}]}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                check.decode_config(body)

    def test_live_requires_real_playlist_and_requested_channels(self):
        valid = '#EXTM3U\n#EXTINF:-1,广东珠江\nhttp://example.com/a\n#EXTINF:-1,广东卫视\nhttp://example.com/b\n'
        for body, success in [(valid, True), ('<html>OK</html>', False),
                              (valid.replace('广东珠江', 'CCTV1'), False),
                              (valid + '#EXTINF:-1,missing', False)]:
            with self.subTest(body=body), patch.object(check, 'fetch', return_value=(body.encode(), check.LIVE_URL)):
                if success:
                    self.assertIn('珠江', check.check_live()['detail'])
                else:
                    with self.assertRaises(ValueError):
                        check.check_live()

    def test_playlist_export_and_failure_gate(self):
        valid = '#EXTM3U\n#EXTINF:-1,广东珠江\nhttp://example.com/a\n#EXTINF:-1,广东卫视\nhttp://example.com/b\n'.encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(check, 'ROOT', root), patch.object(check, 'fetch', return_value=(valid, check.LIVE_URL)) as fetch, \
                    patch.object(check, 'check_vod', side_effect=ValueError('vod failed')), \
                    patch.dict(check.os.environ, {'GITHUB_OUTPUT': str(root / 'output'), 'GITHUB_STEP_SUMMARY': ''}), \
                    patch('builtins.print'):
                self.assertEqual(check.main(), 1)
                self.assertEqual((root / 'build/result.m3u').read_bytes(), valid)
                self.assertIn('live_ready=true', (root / 'output').read_text())
                fetch.return_value = (b'<html>error</html>', check.LIVE_URL)
                (root / 'output').write_text('')
                self.assertEqual(check.main(), 1)
                self.assertFalse((root / 'build/result.m3u').exists())
                self.assertIn('live_ready=false', (root / 'output').read_text())

    def test_vod_checks_jar_and_fallback(self):
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('classes.dex', b'dex\n035\x00test')
        data = jar.getvalue()
        md5 = hashlib.md5(data).hexdigest()
        config = ('{"sites":[{"key":"a","name":"A","type":3,"api":"csp_A"}],'
                  '"spider":"https://example.com/spider;md5;' + md5 + '"}').encode()
        home = b'<div data-clipboard-text="https://example.com/tv"><span>' + '饭太硬'.encode() + b'</span></div>'
        with patch.object(check, 'fetch', side_effect=[(home, check.FAN_HOME), (config, 'https://example.com/tv'), (data, '')]):
            self.assertIn('电视端搜索及影片播放未验证', check.check_vod()['detail'])
        for invalid in (b'<html>OK</html>', data + b'changed'):
            with patch.object(check, 'fetch', side_effect=[(home, check.FAN_HOME), (config, 'https://example.com/tv'), (invalid, '')]):
                with self.assertRaises(ValueError):
                    check.check_vod()
        home += b'<div data-clipboard-text="https://backup.example.com/tv"><span>' + '饭太硬备用'.encode() + b'</span></div>'
        with patch.object(check, 'fetch', side_effect=[(home, check.FAN_HOME), ValueError('primary failed'),
                          (config, 'https://backup.example.com/tv'), (data, '')]):
            self.assertEqual(check.check_vod()['url'], 'https://backup.example.com/tv')

    def test_update_failure_recovery_and_no_churn(self):
        result = dict(url='https://example.com/tv', version='a' * 64, detail='结构通过')
        good = check.update({}, lambda: result, 'time1')
        self.assertEqual(check.update(good, lambda: result, 'time2'), good)
        def fail():
            raise ValueError('failed')
        failed = check.update(good, fail, 'time3')
        self.assertEqual(failed['url'], good['url'])
        self.assertEqual(failed['verified_at'], 'time1')
        self.assertIn('最近检查失败', check.render({'vod': failed}))
        self.assertEqual(check.update(failed, fail, 'time4'), failed)
        recovered = check.update(failed, lambda: result, 'time5')
        self.assertNotIn('error', recovered)
        self.assertEqual(recovered['verified_at'], 'time5')
        self.assertNotIn('url', check.update({}, fail, 'time6'))
        changed = check.update(good, lambda: dict(result, version='b' * 64), 'time7')
        self.assertEqual(changed['updated_at'], 'time7')

    def test_private_urls_rejected(self):
        for url in ('file:///etc/passwd', 'http://user:pass@example.com', 'http://example.com/\nx'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                check.public_url(url)
        with patch.object(check.socket, 'getaddrinfo', return_value=[(0, 0, 0, '', ('127.0.0.1', 80))]):
            with self.assertRaises(ValueError):
                check.public_url('http://example.com')

    def test_end_to_end_output_failure_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = dict(url='https://example.com/tv', version='a' * 64, detail='检查通过')
            with patch.object(check, 'ROOT', root), patch.object(check, 'check_live', return_value=result), \
                    patch.object(check, 'check_vod', return_value=result) as vod, \
                    patch.dict(check.os.environ, {'GITHUB_STEP_SUMMARY': str(root / 'summary'),
                                                 'GITHUB_OUTPUT': str(root / 'output')}), \
                    patch('builtins.print'):
                self.assertEqual(check.main(), 0)
                first = (root / 'README.md').read_bytes(), (root / 'checks.json').read_bytes()
                self.assertEqual(check.main(), 0)
                self.assertEqual(first, ((root / 'README.md').read_bytes(), (root / 'checks.json').read_bytes()))
                vod.side_effect = ValueError('unavailable')
                self.assertEqual(check.main(), 1)
                state = json.loads((root / 'checks.json').read_text())
                self.assertEqual(state['vod']['url'], result['url'])
                self.assertIn('最近检查失败', (root / 'README.md').read_text())
                self.assertIn('vod: failed', (root / 'summary').read_text())
                self.assertIn('ready=true', (root / 'output').read_text())


if __name__ == '__main__':
    unittest.main()
