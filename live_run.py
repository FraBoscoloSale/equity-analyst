"""Create-once historical investment views and separate immutable artifact links."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

DIRECTIONS = ('up', 'down', 'flat', 'not computed')


def atomic_create(path, value):
    """Publish a complete file without replacing an existing name, even in a race."""
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    return atomic_bytes(path, raw)


def atomic_bytes(path, raw):
    path = Path(path)
    if path.exists():
        raise FileExistsError('Historical record already exists; refusing overwrite: '+str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.publish_', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path)  # Atomic create-if-absent; unlike replace, cannot overwrite.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def freeze_view(folder, advisor, now=None):
    folder = Path(folder)
    if (folder/'view.json').exists():
        raise FileExistsError('Historical view already exists; refusing overwrite: '+str(folder/'view.json'))
    direction = advisor.get('direction')
    if direction not in DIRECTIONS:
        raise ValueError('Finalized direction must be one of: '+', '.join(DIRECTIONS))
    # This clock is read at finalization, never from saved run-start metadata.
    finalized = now if now is not None else datetime.now(timezone.utc)
    if finalized.tzinfo is None:
        raise ValueError('Finalized view time must include a timezone')
    run = json.loads((folder/'run.json').read_text(encoding='utf-8'))
    filing = run.get('filing_time') or {}
    return atomic_create(folder/'view.json', {
        'ticker': run['company']['ticker'],
        'filing_accepted_time': filing.get('accepted'),
        'filing_accepted_source': filing.get('accepted_source'),
        'filing_time': filing.get('effective_filing_time'),
        'filing_timezone': filing.get('timezone', 'America/New_York'),
        'time': finalized.isoformat(),
        'original_view': {key: advisor.get(key) for key in ('recommendation', 'thesis', 'reason', 'deciding_evidence_id')},
        'direction': direction, 'reaction': 'not available'})


def reference(folder, kind, artifact):
    """References may be added later; historical view bytes never change."""
    folder, artifact = Path(folder), Path(artifact).resolve()
    view = folder/'view.json'
    if not view.is_file():
        return None  # Legacy saved runs are not assigned a fabricated historical view.
    if kind not in ('verified_report', 'worksheet'):
        raise ValueError('Unknown live artifact reference')
    before = view.read_bytes()
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    value = {'kind': kind, 'path': str(artifact), 'sha256': digest,
             'view_sha256': hashlib.sha256(before).hexdigest()}
    key = hashlib.sha256((str(artifact)+'\0'+digest).encode()).hexdigest()
    target = folder/'live_references'/(kind+'_'+key+'.json')
    if target.exists():
        if json.loads(target.read_text(encoding='utf-8')) != value:
            raise ValueError('Conflicting historical artifact reference')
    else:
        atomic_create(target, value)
    if view.read_bytes() != before:
        raise ValueError('Historical view changed while recording artifact reference')
    return target
