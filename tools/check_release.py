"""Check publication files without printing sensitive matches or contacting a server."""
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {'.pcap', '.pcapng', '.etl', '.sql', '.rdb', '.aof', '.bak',
                      '.exe', '.dll', '.bin', '.log', '.jsonl', '.zip', '.gz', '.pyc'}
FORBIDDEN_DIRS = {'vendor', 'botlogs', 'backups', 'server_profiles', 'managed_leader_states', 'hunt_supply_states'}
SECRET_PATTERNS = (
    re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    re.compile(r'''["'](?:db_password|ssh_password|api_key|access_token)["']\s*:\s*["'][^"'\r\n]+["']'''),
    re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|100\.(?:98|122)\.\d{1,3}\.\d{1,3})\b'),
)


def publication_files(root):
    root = Path(root)
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                                 '--exclude-standard', '-z'], check=True, capture_output=True)
        return sorted(set(root / p.decode('utf-8') for p in result.stdout.split(b'\0') if p))
    return sorted(p for p in root.rglob('*') if p.is_file()
                  and not {'.git', '.venv', '__pycache__'}.intersection(p.relative_to(root).parts))


def scan_file(path, root):
    path, root = Path(path), Path(root)
    relative = path.relative_to(root)
    findings = []
    if path.is_symlink() or path.suffix.lower() in FORBIDDEN_SUFFIXES or FORBIDDEN_DIRS.intersection(relative.parts):
        return ['excluded artifact or link']
    if relative.name in ('server_config.json', 'web_accounts.json', 'managed_bot_accounts.json',
                         'equipment_source_config.json', 'local_auth.py'):
        findings.append('private runtime file')
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeError, OSError):
        return findings + ['non-UTF8 or unreadable file']
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        findings.append('possible secret or deployment address')
    if path.suffix == '.py':
        try:
            ast.parse(text, filename=str(relative))
        except SyntaxError:
            findings.append('Python syntax error')
    if path.suffix == '.json':
        try:
            json.loads(text)
        except ValueError:
            findings.append('invalid JSON')
    return findings


def main():
    failures = []
    files = publication_files(ROOT)
    published = {path.relative_to(ROOT).as_posix() for path in files}
    required = {'README.md', 'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md',
                'simple_bot.py', 'web_console.py', 'protocol_auth.py',
                'tools/init_local.py', 'templates/index.html', 'server/Jiaren.lua'}
    for name in sorted(required - published):
        failures.append(name + ': required publication file missing or ignored')
    for file in files:
        for finding in scan_file(file, ROOT):
            failures.append(f'{file.relative_to(ROOT).as_posix()}: {finding}')
    for message in failures:
        print(message)
    print(f'Publication check: {len(files)} files, {len(failures)} findings')
    return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
