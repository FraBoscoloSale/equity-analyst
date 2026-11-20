"""Fixed pipeline: python -B -u one_desk.py TICKER|screen [--start YYYY-MM-DD].

Replay: python -B -u one_desk.py --replay SAVED_DESK_FOLDER
Saved-input screen: python -B -u one_desk.py screen --saved-screen REPORT --reuse-desk SAVED_DESK_FOLDER
No model selects a successor. Replay reads saved evidence, never the live export.
"""
import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
ORDER = ('Seeker', 'Selection', 'Agent team', 'Worksheet', 'Numeric hook', 'Final report')
SYMBOL = re.compile(r'[A-Za-z][A-Za-z0-9.-]{0,19}')


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=lambda x: x.isoformat() if isinstance(x, (date, datetime)) else str(x)), encoding='utf-8')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PartFailure(RuntimeError):
    pass


@contextmanager
def part(name):
    start = time.monotonic()
    print('[desk] '+name, flush=True)
    try:
        yield
    except Exception as exc:
        raise PartFailure(f'{name}: {exc}') from exc
    finally:
        print(f'[desk] {name} elapsed seconds: {time.monotonic()-start:.3f}', flush=True)


class SavedSEC:
    """One shared SEC transport, with URL-keyed on-disk responses for all parts."""
    def __init__(self, folder, user_agent):
        import spinoff_seeker as seeker
        self.folder = Path(folder)
        self.transport = seeker.SEC(user_agent, archive=self.folder/'sec_sources')

    def relocate(self, folder):
        self.folder = folder
        self.transport.archive = folder/'sec_sources'

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def get(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.folder/'sec_sources'/(key+'.bin')
        meta = path.with_suffix('.json')
        if path.is_file() and meta.is_file():
            import requests
            info = read_json(meta)
            if info['url'] != url or digest(path) != info['sha256']:
                raise ValueError('Saved SEC response does not match URL/hash: '+url)
            response = requests.Response()
            response.status_code, response.url, response._content = 200, url, path.read_bytes()
            response.encoding = 'utf-8'
            return response
        return self.transport.get(url)

    def text(self, url):
        return self.get(url).content.decode('utf-8-sig', errors='replace')

    def json(self, url):
        return json.loads(self.text(url))


@contextmanager
def reader_snapshot(folder, replay=False):
    """Capture the existing reader's export rows; never send them to a model.

    Replay supplies only those saved rows to the unchanged reader's selection,
    ambiguity, currency, date, volume and when-issued rules.
    """
    import closes_reader
    original = closes_reader._export_rows
    location = folder if callable(folder) else lambda: folder
    path = location()/'reader_snapshot.json'
    saved = read_json(path) if path.is_file() else {}
    if replay and not path.is_file():
        raise ValueError('Missing saved reader evidence: reader_snapshot.json')
    if not replay and not path.exists():
        write_json(path, saved)
    def rows(include_volume=False, include_currency=False):
        key = str(bool(include_volume))+'/'+str(bool(include_currency))
        if key not in saved:
            if replay:
                raise ValueError('Missing saved reader evidence: '+key)
            saved[key] = list(original(include_volume=include_volume, include_currency=include_currency))
            write_json(location()/'reader_snapshot.json', saved)
        return iter(saved[key])
    closes_reader._export_rows = rows
    try:
        yield
    finally:
        closes_reader._export_rows = original


def select_screen(folder):
    reports = []
    for path in folder.glob('report_*.md'):
        text = path.read_text(encoding='utf-8')
        stamp = re.search(r'^Run date: ([^|\r\n]+)', text)
        if stamp and text.rstrip().endswith('screens, not recommendations'):
            reports.append((datetime.fromisoformat(stamp[1].strip()), path, text))
    if not reports:
        raise ValueError('No completed seeker report in the desk folder.')
    _, report, text = max(reports, key=lambda row: (row[0], row[1].name))
    registry = read_json(folder/'registry.json')
    selection = screen_pick(text, registry)
    write_json(folder/'screen_selection.json', {'report': report.name, **selection})
    return selection['ticker']


def screen_pick(text, registry):
    """The same pure deterministic choice for live and saved-input screens."""
    section = text.split('## In their window, ordered by rank', 1)[-1].split('\n## ', 1)[0]
    for rank, cik in re.findall(r'^([1-9]\d*)\. .*? \| CIK (\d+) \| total points ', section, re.M):
        candidate = registry.get('candidates', {}).get(cik, {})
        ticker = candidate.get('facts', {}).get('ticker', {}).get('value', '')
        if (candidate.get('eligible') is True and not candidate.get('dropped') and
            candidate.get('rank') == int(rank) and SYMBOL.fullmatch(ticker)):
            return {'cik': cik, 'ticker': ticker.upper()}
    raise ValueError('Empty screen: no ranked eligible name with an established ticker.')


def filing_inventory(folder):
    """List actual saved/read filing documents, including all screen candidates."""
    forms = {}
    registry = read_json(folder/'registry.json')
    for candidate in registry.get('candidates', {}).values():
        for row in candidate.get('filings', []):
            forms[row.get('accession', '')] = row.get('form', '')
    records = []
    if (folder/'filings.json').is_file():
        for label, row in read_json(folder/'filings.json').items():
            accession = row.get('accessionNumber', '')
            forms[accession] = row.get('form', label)
            for filename in (label+'.txt', 'earnings_release.txt' if label == '8-K' else ''):
                if filename and (folder/filename).is_file():
                    records.append(f'{filename} ({forms[accession]} {accession})')
    for meta in (folder/'sec_sources').glob('*.json'):
        url = read_json(meta)['url']
        match = re.search(r'/Archives/edgar/data/\d+/(\d{10})(\d{2})(\d{6})/([^/?]+)$', url, re.I)
        if match:
            accession = '-'.join(match.groups()[:3])
            records.append(f'{match[4]} ({forms.get(accession, "SEC filing")} {accession})')
    # Extra worksheet sources have their own immutable source metadata.
    for meta in (folder/'worksheet_sources').rglob('*.json'):
        row = read_json(meta)
        if row.get('form') and row.get('accession'):
            records.append(f'{meta.with_suffix(".txt").relative_to(folder).as_posix()} ({row["form"]} {row["accession"]})')
    if not records:
        raise ValueError('No saved filing inventory for final first line.')
    return sorted(set(records))


def view_section(worksheet):
    text = worksheet.read_bytes()
    matches = list(re.finditer(rb'^## .*View and Conviction\r?$', text, re.M))
    if len(matches) != 1:
        raise ValueError('Worksheet lacks one final View and Conviction section.')
    start = matches[0].start()
    following = re.search(rb'^## ', text[matches[0].end():], re.M)
    end = matches[0].end()+following.start() if following else len(text)
    return text[start:end]


def compose(folder, manifest):
    return (('Filings read: '+'; '.join(manifest['filings'])+'\n\n').encode()
            +(folder/'verified_report.md').read_bytes()+b'\n'
            +view_section(folder/manifest['worksheet']))


def verify_team(folder):
    """Re-execute existing code checks with saved semantic answers, no API."""
    from desk_verifier import Verifier, load_verifier_sources
    class SavedSemantics:
        index = 0
        def call(self, role, task, context, schema):
            self.index += 1
            stem = folder/f'verifier_semantic_batch_{self.index:03d}'
            expected = read_json(Path(str(stem)+'_input.json'))
            if expected != {'task': task, 'context': context, 'expected_output': schema}:
                raise ValueError('Saved semantic-verifier request differs from required evidence.')
            return read_json(Path(str(stem)+'_output.json'))
    # Scratch verification is a subdirectory, never another independent run.
    with tempfile.TemporaryDirectory(prefix='.numeric_', dir=folder) as tmp, redirect_stdout(io.StringIO()):
        result = Verifier(SavedSemantics()).run(load_verifier_sources(folder), Path(tmp),
                                              instructions=(folder/'verifier_persona.txt').read_text(encoding='utf-8'))
        if result.read_bytes() != (folder/'verified_report.md').read_bytes():
            raise ValueError('Saved verified report differs from offline evidence verification.')


def verify_desk_report(path):
    """Numeric-hook container support: exact composition plus both source audits."""
    import verify_spinoff_worksheet as numeric
    folder = path.parent
    manifest = read_json(folder/'desk_manifest.json')
    for name, expected in manifest['evidence'].items():
        source = (folder/name).resolve()
        if not source.is_relative_to(folder.resolve()) or not source.is_file() or digest(source) != expected:
            raise ValueError('Missing or changed saved desk evidence: '+name)
    if filing_inventory(folder) != manifest['filings']:
        raise ValueError('Desk filing inventory differs from saved evidence.')
    if path.read_bytes() != compose(folder, manifest):
        raise ValueError('Final report differs from the exact saved team report and worksheet view.')
    with reader_snapshot(folder, replay=True):
        numeric.verify(folder/manifest['worksheet'], folder/'spinoff_worksheet.txt')
    verify_team(folder)


class Pipeline:
    """Adapters only: existing seeker, Flow, worksheet and numeric verifier."""
    def __init__(self, settings):
        self.settings = settings

    def seeker(self, folder, client, start, now):
        import spinoff_seeker as seeker
        extractor = seeker.Extractor(self.settings['API_KEY'], cache_dir=folder/'extraction_cache')
        seeker.seek(start, None, now.date(), folder, client, extractor, run_at=now.isoformat())

    def team(self, ticker, folder, client):
        from agent_team import DeskFlow
        return Path(DeskFlow(self.settings, run_folder=folder, sec_client=client).kickoff(inputs={'ticker': ticker}))

    def worksheet(self, folder, client, now):
        import spinoff_worksheet as worksheet
        return worksheet.run_worksheet(folder, read_json(folder/'registry.json'), folder/'spinoff_worksheet.txt',
                    folder/'investment_advisor.txt', worksheet.JudgmentClient(self.settings['API_KEY']), client, now=now)

    def hook(self, path, folder):
        import verify_spinoff_worksheet as numeric
        numeric.verify(path, folder/'spinoff_worksheet.txt')

    def team_hook(self, folder):
        import verify_spinoff_worksheet as numeric
        numeric.verify(folder/'verified_report.md', folder/'spinoff_worksheet.txt')


def run(selection, *, root=ROOT/'runs/desk', start=None, settings=None, pipeline=None, now=None):
    selection = selection.strip()
    if not SYMBOL.fullmatch(selection):
        raise ValueError('Supply one ticker or screen.')
    screen = selection.casefold() == 'screen'
    now = now or datetime.now().astimezone()
    start = start or now.date()-timedelta(days=90)
    if start > now.date():
        raise ValueError('Screen start date cannot be after command start date.')
    print('Fixed execution order: '+' -> '.join(ORDER)+' (Seeker runs only for screen)', flush=True)
    folder = Path(root).resolve()/(selection.upper()+'_'+now.strftime('%Y-%m-%d_%H-%M-%S_%f'))
    folder.mkdir(parents=True, exist_ok=False)
    print('Desk folder: '+str(folder), flush=True)
    if settings is None:
        from dotenv import dotenv_values
        settings = dotenv_values(ROOT/'.env')
    pipeline = pipeline or Pipeline(settings)
    client = SavedSEC(folder, settings.get('SEC_USER_AGENT', ''))
    for source, target in ((ROOT/'spinoff_worksheet.txt', 'spinoff_worksheet.txt'),
                           (ROOT/'roles/investment_advisor.txt', 'investment_advisor.txt'),
                           (ROOT/'roles/verifier.txt', 'verifier_persona.txt'),
                           (ROOT/'spinoff_factors.txt', 'spinoff_factors.txt')):
        shutil.copyfile(source, folder/target)
    registry = ROOT/'spinoff_runs/registry.json'
    write_json(folder/'registry.json', read_json(registry) if registry.is_file() else {'candidates': {}})
    with reader_snapshot(lambda: client.folder):
        with part('Seeker'):
            if screen:
                pipeline.seeker(folder, client, start, now)
            else:
                print('[desk] Seeker not applicable for ticker input', flush=True)
        with part('Selection'):
            ticker = select_screen(folder) if screen else selection.upper()
            if screen:
                destination = folder.with_name(ticker+'_'+now.strftime('%Y-%m-%d_%H-%M-%S_%f'))
                folder.rename(destination)
                folder = destination
                client.relocate(folder)
            print('[desk] Selected ticker: '+ticker, flush=True)
        with part('Agent team'):
            report = pipeline.team(ticker, folder, client)
            if report.resolve() != (folder/'verified_report.md').resolve() or not report.is_file():
                raise ValueError('Team did not return this desk folder\'s verified report.')
        with part('Worksheet'):
            worksheet = Path(pipeline.worksheet(folder, client, datetime.now().astimezone()))
            if worksheet.parent.resolve() != folder.resolve() or not worksheet.is_file():
                raise ValueError('Worksheet must be a saved file in the desk folder.')
        with part('Numeric hook'):
            pipeline.team_hook(folder)
            pipeline.hook(worksheet, folder)
        with part('Final report'):
            manifest = {'ticker': ticker, 'command_start': now.isoformat(), 'order': list(ORDER),
                        'worksheet': worksheet.name, 'filings': filing_inventory(folder)}
            manifest['evidence'] = {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob('*')
                                    if p.is_file() and p.name not in ('desk_manifest.json', 'desk_report.md')}
            write_json(folder/'desk_manifest.json', manifest)
            final = folder/'desk_report.md'
            final.write_bytes(compose(folder, manifest))
            pipeline.hook(final, folder)
            manifest['status'] = 'completed'
            write_json(folder/'desk_manifest.json', manifest)
            print('Desk report: '+str(final), flush=True)
    return final


def replay(folder):
    folder = Path(folder).resolve()
    print('Fixed execution order (saved-file replay): validate evidence -> numeric hook -> reproduce final report', flush=True)
    with part('Replay'):
        if read_json(folder/'desk_manifest.json').get('status') != 'completed':
            raise ValueError('Cannot replay an incomplete desk run.')
        import verify_spinoff_worksheet as numeric
        numeric.verify(folder/'desk_report.md', folder/'spinoff_worksheet.txt')
        manifest = read_json(folder/'desk_manifest.json')
        reproduced = compose(folder, manifest)
        if reproduced != (folder/'desk_report.md').read_bytes():
            raise ValueError('Replay did not reproduce the final report byte-for-byte.')
        print('Desk report: '+str(folder/'desk_report.md'), flush=True)
    return folder/'desk_report.md'


def saved_input_screen(report, folder):
    """Exercise screen -> pick -> desk using existing outputs, without writes.

    The report must match its sibling registry by run timestamp and, when
    printed, factor-file hash. No report is substituted and no live fallback
    exists. Completed desk evidence is validated by the existing numeric hook.
    """
    report, folder = Path(report).resolve(), Path(folder).resolve()
    print('Fixed execution order: '+' -> '.join(ORDER)+' (Seeker runs only for screen)', flush=True)
    print('Saved-input screen execution: local saved evidence; not a fresh live screen.', flush=True)
    with part('Seeker'):
        text = report.read_text(encoding='utf-8')
        stamp = re.search(r'^Run date: ([^|\r\n]+)', text)
        if stamp is None or not text.rstrip().endswith('screens, not recommendations'):
            raise ValueError('Supplied seeker report is not completed.')
        registry = read_json(report.parent/'registry.json')
        if datetime.fromisoformat(stamp[1].strip()) != datetime.fromisoformat(registry.get('updated_at', '')):
            raise ValueError('Saved registry does not match the supplied seeker report run date.')
        spec = re.search(r'Factor file SHA256: ([a-f0-9]{64})\.', text)
        if spec and registry.get('factors', {}).get('sha256') != spec[1]:
            raise ValueError('Saved registry factor specification does not match the supplied report.')
        print('[desk] Reusing completed seeker report: '+str(report), flush=True)
    with part('Selection'):
        ticker = screen_pick(text, registry)['ticker']
        print('[desk] Selected ticker: '+ticker, flush=True)
        manifest = read_json(folder/'desk_manifest.json')
        if manifest.get('status') != 'completed':
            raise ValueError('Reuse requires a completed desk run.')
        run = read_json(folder/'run.json')
        view = read_json(folder/'view.json')
        identities = (manifest.get('ticker'), run.get('company', {}).get('ticker'), view.get('ticker'))
        if any(not isinstance(value, str) or value.upper() != ticker for value in identities):
            raise ValueError('Selected ticker '+ticker+' does not match the supplied completed desk run.')
    with part('Agent team'):
        team_report = folder/'verified_report.md'
        if not team_report.is_file():
            raise ValueError('Completed desk run lacks its saved verified team report.')
        print('[desk] Reusing saved team evidence: '+str(team_report), flush=True)
    with part('Worksheet'):
        worksheet = (folder/manifest['worksheet']).resolve()
        if not worksheet.is_relative_to(folder) or not worksheet.is_file():
            raise ValueError('Completed desk run lacks its saved worksheet in the run folder.')
        print('[desk] Reusing saved worksheet: '+str(worksheet), flush=True)
    import verify_spinoff_worksheet as numeric
    with part('Numeric hook'), reader_snapshot(folder, replay=True):
        numeric.verify(team_report, folder/'spinoff_worksheet.txt')
        numeric.verify(worksheet, folder/'spinoff_worksheet.txt')
    with part('Final report'):
        final = folder/'desk_report.md'
        numeric.verify(final, folder/'spinoff_worksheet.txt')
        if compose(folder, manifest) != final.read_bytes():
            raise ValueError('Saved-input screen did not preserve the final report byte-for-byte.')
        print('Desk report: '+str(final), flush=True)
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('selection', nargs='?')
    parser.add_argument('--start', type=date.fromisoformat, help='Screen distribution cutoff; default: command date minus 90 days')
    parser.add_argument('--output-root', type=Path, default=ROOT/'runs/desk')
    parser.add_argument('--replay', type=Path)
    parser.add_argument('--saved-screen', type=Path, help='Completed local seeker report; requires screen and --reuse-desk')
    parser.add_argument('--reuse-desk', type=Path, help='Completed local desk matching the saved screen selection')
    args = parser.parse_args(argv)
    try:
        if args.saved_screen or args.reuse_desk:
            if (not args.saved_screen or not args.reuse_desk or not args.selection or
                args.selection.strip().casefold() != 'screen' or args.replay or args.start or
                args.output_root != ROOT/'runs/desk'):
                parser.error('Saved-input mode requires screen, --saved-screen and --reuse-desk; no live/replay arguments.')
            saved_input_screen(args.saved_screen, args.reuse_desk)
        elif args.replay:
            if args.selection or args.start:
                parser.error('--replay accepts only an existing desk folder')
            replay(args.replay)
        elif args.selection:
            run(args.selection, root=args.output_root, start=args.start)
        else:
            parser.error('Supply a ticker, screen, or --replay SAVED_DESK_FOLDER')
    except Exception as exc:
        print('Error: '+('' if isinstance(exc, PartFailure) else 'Initialization: ')+str(exc), file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
