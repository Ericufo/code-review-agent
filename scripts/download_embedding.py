"""Explicit one-time download of the pinned CPU model; runtime never downloads."""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_embedding import DEFAULT_MODEL_DIR, FILES, MODEL, REVISION


def main():
    import requests
    for name, expected in FILES.items():
        target = DEFAULT_MODEL_DIR / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(f'Verified {name}')
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.partial')
        try:
            with requests.get(f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}',
                              stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                size, checksum = 0, hashlib.sha256()
                with temporary.open('wb') as output:
                    for data in response.iter_content(1024 * 1024):
                        size += len(data)
                        if size > 100_000_000:
                            raise ValueError('Model download exceeds size limit')
                        output.write(data)
                        checksum.update(data)
            if checksum.hexdigest() != expected:
                raise ValueError('Model download checksum mismatch')
            temporary.replace(target)
            print(f'Downloaded and verified {name}: {size} bytes')
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
