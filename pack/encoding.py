import base64
from pathlib import Path


def file_to_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode('utf-8')


def file_to_base64_uri(path: str) -> str:
    return 'base64://' + file_to_base64(path)
