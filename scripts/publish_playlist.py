"""Publish only a checked playlist; upload and verify before replacing the old asset."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone
import uuid

ROOT = Path(__file__).resolve().parents[1]
TAG = 'playlist-latest'
NAME = 'result.m3u'


def gh(*args):
    return subprocess.check_output(['gh', *args], stderr=subprocess.PIPE, timeout=120)


def api(path, **fields):
    args = ['api', path]
    for key, value in fields.items():
        args += ['-F', f'{key}={value}']
    if fields:
        args += ['--method', 'PATCH']
    return json.loads(gh(*args))


def replace_asset(base, release, path, digest):
    assets = release['assets']
    old = next((a for a in assets if a['name'] == NAME), None)
    # Recover an interrupted rename before attempting another upload.
    backups = [a for a in assets if a['name'].startswith('previous-')]
    if old is None and backups:
        old = max(backups, key=lambda a: a['id'])
        api(f'{base}/releases/assets/{old["id"]}', name=NAME)
        old['name'] = NAME
    def downloaded_hash(asset):
        data = gh('api', f'{base}/releases/assets/{asset["id"]}',
                  '-H', 'Accept: application/octet-stream')
        return hashlib.sha256(data).hexdigest()
    if old and downloaded_hash(old) == digest:
        return False
    with tempfile.TemporaryDirectory() as directory:
        pending = Path(directory) / f'pending-{uuid.uuid4().hex}.m3u'
        pending.write_bytes(path.read_bytes())
        gh('release', 'upload', TAG, str(pending))
        assets = api(f'{base}/releases/{release["id"]}')['assets']
        new = next(a for a in assets if a['name'] == pending.name)
        if downloaded_hash(new) != digest:
            raise ValueError('新附件下载校验失败，保留旧附件')
        if old:
            api(f'{base}/releases/assets/{old["id"]}', name=f'previous-{old["id"]}.m3u')
        try:
            api(f'{base}/releases/assets/{new["id"]}', name=NAME)
        except Exception:
            if old:
                api(f'{base}/releases/assets/{old["id"]}', name=NAME)
            raise
    return True


def main():
    base = f'repos/{os.environ["GITHUB_REPOSITORY"]}'
    path = ROOT / 'build' / NAME
    live = json.loads((ROOT / 'checks.json').read_text())['live']
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if live['status'] != 'ok' or digest != live['version']:
        raise ValueError('附件与本次通过检测的直播列表不一致')
    try:
        release = api(f'{base}/releases/tags/{TAG}')
    except subprocess.CalledProcessError as error:
        if b'HTTP 404' not in error.stderr:
            raise
        gh('release', 'create', TAG, '--draft', '--target', os.environ['GITHUB_SHA'],
           '--title', '最新直播列表', '--notes', '等待直播附件上传完成。')
        release = api(f'{base}/releases/tags/{TAG}')
    changed = replace_asset(base, release, path, digest)
    if changed or release['draft'] or digest not in (release.get('body') or ''):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        api(f'{base}/releases/{release["id"]}', draft='false',
            body=f'最近成功发布：{now}\n\n附件：result.m3u\n\nSHA256：{digest}\n\n'
                 '来自 Guovin，已校验 M3U 格式且包含珠江台和卫视；未逐台验证播放。'
                 '\n\n下载后可传到电视选择本地文件；频道播放仍需联网。')
    # Only clean temporary assets after the stable download is in place.
    for asset in api(f'{base}/releases/{release["id"]}')['assets']:
        if asset['name'].startswith(('pending-', 'previous-')):
            gh('api', f'{base}/releases/assets/{asset["id"]}', '--method', 'DELETE')
    print('result.m3u 已发布并校验' if changed else 'result.m3u 内容未变，无需上传')


if __name__ == '__main__':
    main()
