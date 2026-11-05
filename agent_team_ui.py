"""Local browser interface for the existing CLI: python agent_team_ui.py."""

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import uuid
import webbrowser

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'ui'
SYMBOL = re.compile(r'[A-Za-z][A-Za-z0-9.-]{0,19}')
NODES = ('The seven steps', 'Research Analyst', 'Financial Analyst',
         'Investment Advisor', 'Strategy Manager', 'Verifier')


class InputError(ValueError):
    pass


def command_for(payload, root=ROOT):
    """Translate three form fields to the unchanged CLI, never a shell string."""
    if not isinstance(payload, dict) or set(payload) - {'ticker', 'peers', 'price'}:
        raise InputError('Supply only ticker, optional peers, and optional price.')
    ticker = payload.get('ticker', '')
    peers = payload.get('peers', '')
    if not isinstance(ticker, str) or not SYMBOL.fullmatch(ticker.strip()):
        raise InputError('Enter a ticker using letters, numbers, dots or hyphens, starting with a letter.')
    if not isinstance(peers, str):
        raise InputError('Enter peers separated by commas or spaces.')
    peers = list(dict.fromkeys(p.upper() for p in re.split(r'[,\s]+', peers.strip()) if p))
    if len(peers) > 20 or any(not SYMBOL.fullmatch(p) for p in peers):
        raise InputError('Enter up to 20 valid peer tickers, separated by commas or spaces.')
    command = [sys.executable, '-u', str(root / 'agent_team.py'), ticker.strip().upper()]
    if peers:
        command += ['--peers', ','.join(peers)]
    price = payload.get('price')
    if price is not None and price != '':
        try:
            if isinstance(price, bool):
                raise ValueError
            price = float(price)
            if not math.isfinite(price) or price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise InputError('Price must be a finite positive number, or left blank.') from None
        command += ['--price', str(price)]
    return command


def node_from_line(line):
    line = line.strip()
    if line.startswith('Step ') and re.match(r'Step [1-7]/7: ', line):
        return line
    if line.startswith('Verifier: code checks'):
        return 'Verifier'
    # CrewAI's console frames can interleave with the existing flushed print.
    for node in NODES:
        if re.search(r'(?:^|[\s│])' + re.escape(node) + r'\s*$', line):
            return node
    return None


class RunService:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None

    def scrubber(self):
        # Values stay entirely on the server, including errors from dependencies.
        values = {str(v).strip() for v in dotenv_values(self.root / '.env').values() if v}
        values.update(str(v).strip() for k, v in os.environ.items()
                      if v and re.search(r'KEY|TOKEN|SECRET|PASSWORD|USER_AGENT', k, re.I))
        ordered = sorted((v for v in values if v), key=len, reverse=True)
        def scrub(text):
            for value in ordered:
                text = text.replace(value, '[redacted]')
            return text
        return scrub

    def submit(self, payload):
        command = command_for(payload, self.root)
        with self.lock:
            if self.active:
                raise InputError('A run is already active. Wait for it to finish.')
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'status': 'running', 'nodes': [], 'error': '',
                   'report': '', 'counts': {}, 'unresolved_parts': 0}
            self.jobs[job_id] = job
            self.active = job_id
            # Retain recent results through browser refresh without unbounded storage.
            while len(self.jobs) > 8:
                del self.jobs[next(iter(self.jobs))]
            threading.Thread(target=self.execute, args=(job_id, command), daemon=True).start()
            return job_id

    def snapshot(self, job_id=None):
        with self.lock:
            if job_id is None:
                job_id = self.active or next(reversed(self.jobs), None)
            if job_id not in self.jobs:
                return None
            return json.loads(json.dumps(self.jobs[job_id]))

    def verified_artifacts(self, report_path):
        path = Path(report_path).resolve()
        research_root = (self.root / 'research_data').resolve()
        if not path.is_relative_to(research_root) or path.name != 'verified_report.md':
            raise ValueError('The run did not return a valid verified-report location.')
        folder = path.parent
        report = path.read_text(encoding='utf-8')
        if not report.strip() or not (folder / 'compiled_report.md').is_file():
            raise ValueError('The run did not produce a complete verified report.')
        semantic = json.loads((folder / 'verifier_semantic_results.json').read_text(encoding='utf-8'))
        plan = json.loads((folder / 'verifier_semantic_batches.json').read_text(encoding='utf-8'))
        expected = plan['expected_ids']
        if (semantic.get('complete') is not True or not isinstance(expected, list) or
            len(expected) != len(set(expected)) or not isinstance(semantic.get('checks'), dict) or
            set(semantic['checks']) != set(expected) or semantic.get('aliases') != plan.get('aliases')):
            raise ValueError('Verification is incomplete: semantic coverage was not confirmed.')
        marks = json.loads((folder / 'verification_marks.json').read_text(encoding='utf-8'))
        if not isinstance(marks, list) or any(row.get('mark') not in {'correct', 'wrong', 'unsupported'} for row in marks):
            raise ValueError('The run did not produce valid verification marks.')
        review = json.loads((folder / 'review_final.json').read_text(encoding='utf-8'))
        unresolved = sum(not check.get('passed') for check in review['checks'].values())
        return report, dict(Counter(row['mark'] for row in marks)), unresolved

    def execute(self, job_id, command):
        scrub = lambda text: text
        try:
            scrub = self.scrubber()
            report_path, error = None, ''
            environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
            # This is the sole analysis entry point. No duplicate Flow or API calls.
            with subprocess.Popen(command, cwd=self.root, env=environment, shell=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  encoding='utf-8', errors='replace', bufsize=1,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
                for raw in process.stdout:
                    line = raw.strip()
                    node = node_from_line(line)
                    with self.lock:
                        nodes = self.jobs[job_id]['nodes']
                        if node and (not nodes or nodes[-1] != node):
                            nodes.append(node)
                    if line.startswith('Error:'):
                        error = line
                    if line.startswith('Verified report: '):
                        report_path = line.removeprefix('Verified report: ').strip()
                code = process.wait()
            if code != 0:
                if not error:
                    # Never expose a raw traceback or source/model log to the page.
                    error = f'Agent team exited with code {code}. No completed verified report was returned.'
                raise RuntimeError(error)
            if not report_path:
                raise ValueError('The agent team ended without returning a verified report. The run is incomplete.')
            report, counts, unresolved = self.verified_artifacts(report_path)
            with self.lock:
                self.jobs[job_id].update(status='completed', report=scrub(report), counts=counts,
                                         unresolved_parts=unresolved)
        except Exception as exc:
            # Include actionable CLI errors, but not environment values or traces.
            if isinstance(exc, (RuntimeError, ValueError)):
                message = str(exc)
            elif isinstance(exc, FileNotFoundError):
                message = 'A required program or verification artifact is missing. The run is incomplete.'
            else:
                message = f'The run could not finish ({type(exc).__name__}). No completed verified report is available.'
            with self.lock:
                self.jobs[job_id].update(status='failed', error=scrub(message), report='')
        finally:
            with self.lock:
                if self.active == job_id:
                    self.active = None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        super().__init__(address, Handler)
        self.service = service
        self.form_token = secrets.token_urlsafe(32)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log user inputs, report contents, or environment values.

    def send(self, code, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        # Bind to loopback and reject rebinding/cross-site form submissions.
        return self.headers.get('Host') == expected

    def do_GET(self):
        if not self.local_request():
            return self.send(403, {'error': 'Open the local page using the printed address.'})
        path = urlsplit(self.path).path
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path in assets:
            filename, mime = assets[path]
            return self.send(200, (ASSETS / filename).read_bytes(), mime)
        if path == '/api/session':
            return self.send(200, {'token': self.server.form_token, 'run': self.server.service.snapshot()})
        if re.fullmatch(r'/api/runs/[a-f0-9]{32}', path):
            job = self.server.service.snapshot(path.rsplit('/', 1)[1])
            return self.send(200, job) if job else self.send(404, {'error': 'Run not found.'})
        self.send(404, {'error': 'Not found.'})

    def do_POST(self):
        if (not self.local_request() or self.headers.get('X-Run-Token') != self.server.form_token or
            self.headers.get('Origin') not in (None, f'http://127.0.0.1:{self.server.server_port}')):
            return self.send(403, {'error': 'Reload the local page before starting a run.'})
        if self.path != '/api/runs':
            return self.send(404, {'error': 'Not found.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise InputError('The run form is empty or too large.')
            payload = json.loads(self.rfile.read(length))
            job_id = self.server.service.submit(payload)
        except (ValueError, UnicodeError):
            # InputError is safe, constructed locally; never echo request bodies.
            exc = sys.exc_info()[1]
            message = str(exc) if isinstance(exc, InputError) else 'Enter a valid run form.'
            return self.send(400, {'error': message})
        self.send(202, {'id': job_id})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8501)
    args = parser.parse_args()
    with LocalServer(('127.0.0.1', args.port), RunService()) as server:
        url = f'http://127.0.0.1:{server.server_port}'
        print(f'Agent team page: {url}', flush=True)
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
