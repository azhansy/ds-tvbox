"""Check upstream subscriptions; never execute downloaded TVBox plugins."""
import base64
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
import zipfile

import json5

LIVE_URL = 'https://github.com/Guovin/iptv-api/releases/download/playlist-latest/result.m3u'
FAN_HOME = 'https://www.xn--sss604efuw.net/'
ROOT = Path(__file__).resolve().parents[1]
LIMIT = 16 * 1024 * 1024


def public_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError('不是公开 HTTP(S) 地址')
    host = parts.hostname.encode('idna').decode('ascii')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    addresses = sorted({x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ValueError('拒绝本机或私网地址')
    authority = f'[{host}]' if ':' in host else host
    if parts.port:
        authority += f':{port}'
    normalized = urlunsplit((parts.scheme, authority, parts.path, parts.query, ''))
    return normalized, host, port, addresses[0]


def fetch(url):
    # Bound redirects, wall time and body size; pin DNS for direct connections.
    with tempfile.TemporaryDirectory() as directory:
        body, headers = Path(directory) / 'body', Path(directory) / 'headers'
        for _ in range(6):
            url, host, port, address = public_url(url)
            address = f'[{address}]' if ':' in address else address
            result = subprocess.run([
                'curl', '--disable', '--silent', '--show-error', '--max-time', '30',
                '--connect-timeout', '10', '--max-filesize', str(LIMIT),
                '--proto', '=http,https', '--resolve', f'{host}:{port}:{address}',
                '--user-agent', 'okhttp/4.12.0', '--dump-header', str(headers),
                '--output', str(body), '--write-out', '%{http_code}', url,
            ], capture_output=True, timeout=35, check=False)
            if result.returncode:
                raise ValueError(f'下载失败（curl {result.returncode}）')
            status = int(result.stdout)
            if status in (301, 302, 303, 307, 308):
                locations = re.findall(r'^location:\s*(.+?)\r?$', headers.read_text(encoding='latin-1'), re.I | re.M)
                if not locations:
                    raise ValueError('跳转缺少地址')
                url = urljoin(url, locations[-1].strip())
                continue
            if status != 200 or not body.exists() or not 0 < body.stat().st_size <= LIMIT:
                raise ValueError(f'下载无效（HTTP {status}）')
            return body.read_bytes(), url
    raise ValueError('跳转次数过多')


class FanLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.url = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        value = dict(attrs).get('data-clipboard-text')
        if value:
            self.url = value

    def handle_data(self, data):
        if self.url and data.strip() in ('饭太硬', '饭太硬备用'):
            self.links.append(self.url)
            self.url = None

    def handle_endtag(self, tag):
        if tag == 'div':
            self.url = None


def decode_config(body):
    if not body.lstrip().startswith(b'{'):
        # TVBox image wrapper: image bytes + ** + base64(JSON/JSON5).
        if b'**' not in body:
            raise ValueError('不是 TVBox 配置或支持的图片封装')
        body = base64.b64decode(body.rsplit(b'**', 1)[1].strip(), validate=True)
    config = json5.loads(body.decode('utf-8-sig'), allow_duplicate_keys=False)
    if not isinstance(config, dict) or not isinstance(config.get('sites'), list) or not config['sites']:
        raise ValueError('配置没有点播站点')
    keys = set()
    for site in config['sites']:
        if (not isinstance(site, dict) or not isinstance(site.get('key'), str)
                or not site['key'] or site['key'] in keys
                or not isinstance(site.get('name'), str) or not site['name']
                or not isinstance(site.get('api'), str) or not site['api']
                or type(site.get('type')) is not int
                or site.get('type') not in (0, 1, 3, 4)):
            raise ValueError('点播站点格式无效或重复')
        keys.add(site['key'])
    return config


def check_live(output=None):
    body, _ = fetch(LIVE_URL)
    lines = [line.strip() for line in body.decode('utf-8-sig').splitlines() if line.strip()]
    if not lines or not lines[0].startswith('#EXTM3U'):
        raise ValueError('直播返回的不是 M3U')
    names, pending = [], None
    for line in lines[1:]:
        if line.startswith('#EXTINF:'):
            if pending is not None or ',' not in line:
                raise ValueError('频道缺少名称或播放地址')
            pending = line.rsplit(',', 1)[1]
        elif not line.startswith('#'):
            if not pending or urlsplit(line).scheme not in ('http', 'https', 'rtsp', 'rtmp', 'udp', 'rtp'):
                raise ValueError('频道播放地址格式无效')
            names.append(pending)
            pending = None
    if pending is not None or not names or not any('珠江' in n for n in names) or not any('卫视' in n for n in names):
        raise ValueError('列表不完整，或缺少珠江台/卫视')
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(body)
    return dict(url=LIVE_URL, version=hashlib.sha256(body).hexdigest(),
                detail=f'M3U 格式通过，{len(names)} 条线路，含珠江台和卫视；频道播放由上游检测，本项目未逐台验证')


def check_vod():
    home, _ = fetch(FAN_HOME)
    parser = FanLinks()
    parser.feed(home.decode('utf-8'))
    if not parser.links:
        raise ValueError('饭太硬导航页没有找到订阅入口')
    for url in dict.fromkeys(parser.links):
        try:
            body, final = fetch(url)
            config = decode_config(body)
            spider = config.get('spider')
            if not isinstance(spider, str) or not spider:
                raise ValueError('缺少点播插件地址')
            jar_url, *checksum = spider.split(';md5;')
            jar, _ = fetch(urljoin(final, jar_url))
            if checksum and (len(checksum) != 1 or hashlib.md5(jar).hexdigest() != checksum[0].lower()):
                raise ValueError('点播插件 MD5 不匹配')
            with zipfile.ZipFile(io.BytesIO(jar)) as archive:
                info = archive.getinfo('classes.dex')
                if info.file_size > LIMIT:
                    raise ValueError('点播插件体积异常')
                dex = archive.read(info)
                if not dex.startswith(b'dex\n'):
                    raise ValueError('点播插件不是有效 DEX')
            return dict(url=url, version=hashlib.sha256(body + jar).hexdigest(),
                        detail=f'配置解析、{len(config["sites"])} 个站点结构、插件 DEX'
                        + (' 和 MD5 校验通过' if checksum else ' 检查通过（上游未提供 MD5）')
                        + '；电视端搜索及影片播放未验证')
        except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError):
            continue
    raise ValueError('饭太硬主/备用入口配置或插件检测失败')


def update(previous, checker, now):
    try:
        result = dict(checker(), status='ok')
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError) as error:
        result = dict(previous, status='failed', error=str(error))
    comparison = {k: v for k, v in previous.items() if k not in ('updated_at', 'verified_at')}
    current = {k: v for k, v in result.items() if k not in ('updated_at', 'verified_at')}
    if comparison == current:
        return previous
    result['updated_at'] = now
    if result['status'] == 'ok':
        result['verified_at'] = now
    return result


def render(state):
    lines = ['# TVBox 订阅地址', '', '复制下面两个地址到 TVBox 对应设置中。订阅内容由上游维护。', '']
    for key, title, setting in [('live', '直播 · Guovin', '设置 → 直播 → 直播地址'),
                                 ('vod', '点播 · 饭太硬', '设置 → 点播 → 配置地址')]:
        item = state.get(key, {})
        lines += [f'## {title}', '', f'填写位置：{setting}（不同客户端名称可能略有不同）。', '']
        if item.get('url'):
            lines += ['```text', item['url'], '```', '']
        else:
            lines += ['尚无通过检测的地址。', '']
        if item.get('status') == 'failed':
            lines += ['**最近检查失败；已有地址仅为上次通过记录，当前可用性未确认。**', '']
        if item.get('detail'):
            lines += [item['detail'] + '。', '', f'此版本通过时间：{item["verified_at"]}。', '',
                      f'内容指纹：`{item["version"][:12]}`。', '']
    lines += ['## 下载直播文件', '',
              '[下载 result.m3u](https://github.com/azhansy/ds-tvbox/releases/download/playlist-latest/result.m3u) · '
              '[查看发布页和更新时间](https://github.com/azhansy/ds-tvbox/releases/tag/playlist-latest)', '',
              '下载后传到电视或 U 盘，在支持本地直播文件的客户端中选择它。播放频道仍需联网，本地文件需手动重新下载更新。', '',
              '附件是上次成功发布的直播列表，更新时间以发布页为准；直播检测或上传失败时保留旧附件。', '',
              '## 自动更新', '',
              '- 直播、点播检测及 Release 同步每两天执行一次，也可在 Actions 页面手动运行 Refresh subscriptions。',
              '- Action 每日 UTC 19:17（北京时间次日 03:17）判断日期，按连续 UTC 天数隔天执行，跨月不重置；非执行日不访问订阅源。GitHub 排队可能延迟运行。',
              '- 直播直接使用 Guovin 的 result.m3u；不再自行采集、合并或筛选频道。',
              '- 直播列表检测通过后同步到本仓库的固定 Release，附件为 result.m3u；内容不变时不重复上传。',
              '- 点播从[饭太硬导航页](https://www.xn--sss604efuw.net/)发现主/备用入口，检查配置和插件后才写入地址。',
              '- 仅内容或检测状态变化时提交 README 和检查记录；失败保留上次地址、标记异常并让 Action 报错。每次运行时间和结果见 Action 摘要。',
              '- 本项目不执行点播插件。配置检查通过不等于全部影片能播放；电视端效果取决于客户端和所在网络。',
              '- 固定地址由上游直接更新，不是本项目保存的快照；上游变动与下次检查之间可能存在时间差。',
              '', '[Guovin 上游](https://github.com/Guovin/iptv-api) · '
              '[直播发布页](https://github.com/Guovin/iptv-api/releases/tag/playlist-latest)', '',
              '本地检查：`python -m pip install --require-hashes -r requirements.lock`，'
              '`python -m unittest discover -s tests`，`python scripts/check_sources.py`。', '']
    return '\n'.join(lines)


def main():
    path = ROOT / 'checks.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    playlist = ROOT / 'build' / 'result.m3u'
    playlist.unlink(missing_ok=True)
    state = {key: update(state.get(key, {}), checker, now)
             for key, checker in [('live', lambda: check_live(playlist)), ('vod', check_vod)]}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    (ROOT / 'README.md').write_text(render(state))
    summary = f'检查时间：{now}\n\n' + '\n'.join(
        f'- {key}: {item["status"]} — {item.get("error", item.get("detail", ""))}'
        for key, item in state.items()) + '\n'
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(summary)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('ready=true\n')
            output.write(f'live_ready={str(state["live"]["status"] == "ok" and playlist.exists()).lower()}\n')
    return int(any(item['status'] != 'ok' for item in state.values()))


if __name__ == '__main__':
    raise SystemExit(main())
