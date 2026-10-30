"""Initialize empty local settings, without contacting a server or overwriting data."""
import json
from pathlib import Path


def initialize(base):
    base = Path(base)
    created = []
    for example in sorted((base / 'examples').glob('*.example.json')):
        target = base / example.name.replace('.example.json', '.json')
        data = json.loads(example.read_text(encoding='utf-8'))
        try:
            with target.open('x', encoding='utf-8') as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write('\n')
        except FileExistsError:
            continue
        created.append(target.name)
    return created


if __name__ == '__main__':
    for name in initialize(Path(__file__).resolve().parents[1]):
        print('Created ' + name)
