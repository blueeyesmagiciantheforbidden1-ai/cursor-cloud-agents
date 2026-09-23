"""Read downloaded ELF metadata without executing a Linux binary."""
import gzip
import json
from pathlib import Path
import re
import tarfile

def versions(body):
    found = {item.decode() for item in re.findall(rb'GLIBC_[0-9]+(?:\.[0-9]+)+', body)}
    return sorted(found, key=lambda item: tuple(int(x) for x in item[6:].split('.')))

def main():
    root = Path(__file__).resolve().parent
    with tarfile.open(root.parent / 'copilot-worker/.cache/copilot-linux-x64.tar.gz') as archive:
        copilot = archive.extractfile('copilot').read()
    grok = (root / '.cache/grok').read_bytes()
    print(json.dumps({name: {'linux_x86_64_elf': body[:6] == b'\x7fELF\x02\x01' and body[18:20] == b'\x3e\x00',
                             'glibc_versions_referenced': versions(body)}
                      for name, body in [('copilot', copilot), ('grok', grok)]}))

if __name__ == '__main__':
    main()
