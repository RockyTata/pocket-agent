#!/usr/bin/env python3
"""Pocket Agent 的网页服务：只用 Python 自带库，不装任何依赖。

  python3 server.py              默认监听 0.0.0.0:8765（手机在同一个局域网就能打开）
  python3 server.py --port 9000  换端口

口令放在 data/token（第一次启动自动生成并打印出来）。这个服务能让 AI 在你电脑上改文件、跑命令，
所以所有接口都要口令；只有页面本身、静态脚本和上传的附件（文件名带随机串）不要。
"""
import argparse, json, mimetypes, os, re, secrets, socket, sys, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import console

TOKEN_FILE = os.path.join(console.DATA, 'token')
# 页面只能拿到这几个文件，data/ 下的对话记录、口令文件一律不当静态文件给出去
STATIC = {'index.html', 'theme.js', 'gesture.js', 'icons.js', 'sfx.js', 'manifest.json', 'icon.svg'}


def load_token():
    try:
        with open(TOKEN_FILE, encoding='utf-8') as f:
            t = f.read().strip()
            if t: return t, False
    except FileNotFoundError: pass
    t = secrets.token_urlsafe(12)
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f: f.write(t + '\n')
    os.chmod(TOKEN_FILE, 0o600)
    return t, True


def lan_ip():
    # 不真的发包：UDP connect 只是让系统选一块出口网卡，拿它的地址给手机用
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(('10.255.255.255', 1)); return s.getsockname()[0]
    except OSError: return '127.0.0.1'
    finally: s.close()


class H(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

    def end_headers(self):
        if self.path.split('?')[0].endswith(('.html', '.js', '/')): self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b))); self.send_header('Cache-Control', 'no-store'); self.end_headers()
        self.wfile.write(b)

    def authed(self):
        got = self.headers.get('X-Chat-Key', '')
        if not got:
            # <img>/<video> 带不了自定义头，页面在口令验证通过后把它写进 cookie
            m = re.search(r'(?:^|;\s*)pa_key=([^;]+)', self.headers.get('Cookie', ''))
            got = unquote(m.group(1)) if m else ''
        return secrets.compare_digest(got, TOKEN)

    def body(self, limit=200_000):
        n = int(self.headers.get('Content-Length') or 0)
        if n > limit: raise ValueError('too large')
        return json.loads(self.rfile.read(n) or b'{}')

    def send_file(self, fp):
        # 支持 Range：iPhone 的 Safari 没有 Range 就拖不动视频进度条，有时干脆不播
        size = os.path.getsize(fp)
        ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
        start, end = 0, size - 1
        m = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
        if m and (m.group(1) or m.group(2)):
            if m.group(1): start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else end
            else: start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206); self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else: self.send_response(200)
        self.send_header('Content-Type', ctype); self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1)); self.end_headers()
        with open(fp, 'rb') as f:
            f.seek(start); left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk: break
                try: self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError): break
                left -= len(chunk)

    def do_HEAD(self): return self.send_json({'error': 'no'}, 405)

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        if path in ('/', '/index.html'): return self.send_file(os.path.join(HERE, 'index.html'))
        name = path.lstrip('/')
        if name in STATIC and os.path.isfile(os.path.join(HERE, name)): return self.send_file(os.path.join(HERE, name))
        # 上传的附件：文件名是时间+随机串，猜不到；<img> 不带口令也要能显示
        if path.startswith('/data/files/'):
            fn = os.path.basename(path)
            fp = os.path.join(console.FILES, fn)
            if re.fullmatch(r'[\w.\-]+', fn) and os.path.isfile(fp): return self.send_file(fp)
            return self.send_json({'error': 'no'}, 404)
        if not self.authed(): return self.send_json({'error': '口令不对'}, 403)
        # AI 回复里提到的项目内图片/视频：只放行配置的项目根目录下、媒体扩展名的文件
        if path.startswith('/f/'):
            fp = os.path.realpath('/' + path[3:])
            ok = any(fp.startswith(r + os.sep) for r in console.ROOTS) and re.search(r'\.(png|jpe?g|webp|gif|mp4|mov)$', fp, re.I)
            if ok and os.path.isfile(fp): return self.send_file(fp)
            return self.send_json({'error': 'no'}, 404)
        q = parse_qs(u.query)
        if path == '/api/console/state': return self.send_json(console.state())
        if path == '/api/console/thread':
            return self.send_json(console.thread(q.get('id', [''])[0], int((q.get('after') or ['0'])[0] or 0)))
        return self.send_json({'error': 'unknown'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith('/api/console/'): return self.send_json({'error': 'unknown'}, 404)
        if not self.authed(): return self.send_json({'error': '口令不对'}, 403)
        if path == '/api/console/upload': return self.upload()
        try: b = self.body()
        except Exception: return self.send_json({'error': 'bad body'}, 400)
        return self.send_json(console.act(path.rsplit('/', 1)[1], b))

    def upload(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 60 * 1024 * 1024: return self.send_json({'error': '文件太大（上限 60MB）'}, 413)
        name = re.sub(r'[^\w.\-]', '_', unquote(self.headers.get('X-Name', 'file')))[-50:]
        fn = time.strftime('%m%d-%H%M%S-') + secrets.token_hex(4) + '-' + name
        with open(os.path.join(console.FILES, fn), 'wb') as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk: break
                f.write(chunk); left -= len(chunk)
        return self.send_json({'ok': True, 'path': 'data/files/' + fn})


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Pocket Agent server')
    ap.add_argument('--host', default=console.CFG.get('host', '0.0.0.0'))
    ap.add_argument('--port', type=int, default=int(console.CFG.get('port', 8765)))
    a = ap.parse_args()
    TOKEN, new = load_token()
    console.reset_stale()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((a.host, a.port), H)
    host = lan_ip() if a.host in ('0.0.0.0', '') else a.host
    print(f'Pocket Agent running: http://{host}:{a.port}/', flush=True)
    print(f'Token: {TOKEN}' + ('   (new, saved in data/token)' if new else '   (from data/token)'), flush=True)
    print('Project roots: ' + ', '.join(console.ROOTS), flush=True)
    srv.serve_forever()
