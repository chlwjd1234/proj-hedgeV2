from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import gzip
import hashlib
import json
import os
import platform
import subprocess
import uuid
import io
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import ValidationError, Unavailable

D = decimal.Decimal
decimal.getcontext().prec = 60
UTC = dt.timezone.utc


def dec(x: Any) -> D:
    if isinstance(x, float):
        x = repr(x)
    value = D(x)
    if not value.is_finite():
        raise ValidationError('nonfinite financial number')
    return value


def jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return jsonable(dataclasses.asdict(obj))
    if isinstance(obj, D):
        return str(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def canonical(obj: Any) -> bytes:
    return json.dumps(jsonable(obj), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    return sha256(canonical(obj))


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def git_blob_hash(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def write_json(path: Path, obj: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(jsonable(obj), sort_keys=True, indent=2,
                      ensure_ascii=False, allow_nan=False) + '\n'
    if exclusive:
        with path.open('x', encoding='utf8') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    else:
        tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            tmp.write_text(data, encoding='utf8')
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    with path.open(encoding='utf8') as f:
        return json.load(f)


class _OwnedText(io.TextIOWrapper):
    def __init__(self,compressed,backing):
        self._backing=backing
        super().__init__(compressed,encoding='utf8')
    def close(self):
        try:super().close()
        finally:self._backing.close()


def open_text(path: Path, mode: str = 'rt'):
    if path.suffix == '.gz':
        if mode.startswith(('w','x')):
            backing=path.open(mode[0]+'b')
            return _OwnedText(gzip.GzipFile(filename='',mode='wb',fileobj=backing,mtime=0),backing)
        return gzip.open(path, mode, encoding='utf8')
    if path.suffix == '.lz4':
        try:
            import lz4.frame
        except ImportError as exc:
            raise Unavailable('LZ4 archive requires pip install -e ".[archive]"') from exc
        return lz4.frame.open(path, mode, encoding='utf8')
    if path.suffix in ('.zst', '.zstd'):
        try:
            import zstandard
        except ImportError as exc:
            raise Unavailable('Zstandard archive requires pip install -e ".[archive]"') from exc
        return zstandard.open(path, mode, encoding='utf8')
    return path.open(mode, encoding='utf8')


def read_jsonl(path: Path) -> Iterator[dict]:
    with open_text(path) as f:
        for line_no, line in enumerate(f, 1):
            if line.strip():
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('row is not an object')
                    yield row
                except (ValueError, TypeError) as exc:
                    raise ValidationError(f'{path.name}:{line_no}: {exc}') from exc


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open_text(path, 'wt') as f:
        for row in rows:
            f.write(canonical(row).decode() + '\n')
            n += 1
    return n


def iso_ms(value: str | int) -> int:
    if isinstance(value, int):
        return value
    t = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValidationError('timestamps must include a timezone')
    return int(t.timestamp() * 1000)


def utc_iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace('+00:00', 'Z')


def safe_path(root: Path, relative: str) -> Path:
    p = (root / relative).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValidationError(f'path escapes dataset root: {relative}')
    return p


def source_fingerprint(root: Path) -> str:
    files = []
    for sub in ('src', 'configs', 'scripts', 'reference', 'schemas', 'tests'):
        for p in sorted((root / sub).rglob('*')):
            if (p.is_file() and not {'__pycache__', 'node_modules', '.pytest_cache'}.intersection(p.parts)
                    and not any(part.endswith('.egg-info') for part in p.relative_to(root).parts)
                    and p.suffix not in ('.pyc', '.log') and p.name not in ('installed.json', 'npm_install_receipt.json', 'installed_dependencies.json')):
                files.append((str(p.relative_to(root)), hash_file(p)))
    for name in ('pyproject.toml', 'run_pipeline.sh'):
        if (root / name).exists():
            files.append((name, hash_file(root / name)))
    return digest(files)


class Run:
    """Write-once manifests, content-addressed inputs, mutable *pointers* only."""
    def __init__(self, root: Path, stage: str, config: dict, inputs: dict[str, Path] | None = None):
        self.root = root.resolve()
        stamp = dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        self.id = stage + '_' + stamp + '_' + uuid.uuid4().hex[:8]
        self.path = self.root / 'runs' / self.id
        self.path.mkdir(parents=True, exist_ok=False)
        try:
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
            dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True))
        except (OSError, subprocess.SubprocessError):
            sha, dirty = None, None
        self.metadata = {
            'schema_version': 1, 'run_id': self.id, 'stage': stage,
            'git_sha': sha, 'tracked_dirty': dirty,
            'source_fingerprint': source_fingerprint(root), 'config': config,
            'software': {'python': platform.python_version(), 'platform': platform.platform()},
            'inputs': {k: {'path': str(v), 'sha256': hash_file(v)} for k, v in (inputs or {}).items()},
        }
        write_json(self.path / 'run_started.json', self.metadata, exclusive=True)

    def finish(self, result: dict) -> Path:
        # A result cannot be bound to the starting fingerprint if source changed
        # while it was computed. Persist a failure receipt rather than a stale PASS.
        ending_fingerprint = source_fingerprint(self.root)
        if ending_fingerprint != self.metadata['source_fingerprint']:
            result = {**result, 'status': 'FAIL', 'historical_gate': 'FAIL',
                      'economic_evidence': False, 'd3_eligible': False,
                      'strategy_success': False, 'reason': 'SOURCE_CHANGED_DURING_RUN'}
            self.metadata['ending_source_fingerprint'] = ending_fingerprint
        write_json(self.path / 'result.json', result, exclusive=True)
        artifacts = {str(p.relative_to(self.path)): hash_file(p)
                     for p in sorted(self.path.rglob('*')) if p.is_file()}
        write_json(self.path / 'manifest.json', {**self.metadata, 'artifacts': artifacts}, exclusive=True)
        write_json(self.root / 'runs' / (self.metadata['stage'] + '_latest.json'),
                   {'run_id': self.id, 'manifest_sha256': hash_file(self.path / 'manifest.json')})
        return self.path


def verify_run(path: Path) -> dict:
    m = read_json(path / 'manifest.json')
    for rel, expected in m['artifacts'].items():
        p = safe_path(path, rel)
        if not p.is_file() or hash_file(p) != expected:
            raise ValidationError(f'run artifact hash mismatch: {rel}')
    return m
