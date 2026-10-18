"""Content-addressed raw responses with metadata written last."""

from pathlib import Path

from geo_data.common import atomic_write, digest_bytes, encode_json, read_json, write_json


def save_response(directory, payload, metadata):
    directory = Path(directory)
    data = encode_json(payload)
    checksum = digest_bytes(data)
    filename = checksum + ".json"
    atomic_write(directory / filename, data)
    record = dict(metadata, rawFile=filename, sha256=checksum, bytes=len(data))
    write_json(directory / "record.json", record)
    return record


def load_response(directory, request_key):
    directory = Path(directory)
    record_path = directory / "record.json"
    if not record_path.exists():
        return None
    record = read_json(record_path)
    if record.get("requestKey") != request_key:
        raise ValueError("Cache request identity mismatch; use a new output directory")
    filename = record.get("rawFile", "")
    checksum = record.get("sha256", "")
    if len(checksum) != 64 or filename != checksum + ".json" or not all(c in "0123456789abcdef" for c in checksum):
        raise ValueError("Invalid cache filename/checksum")
    data = (directory / filename).read_bytes()
    if digest_bytes(data) != checksum or len(data) != record.get("bytes"):
        raise ValueError("Cached response checksum mismatch")
    return read_json(directory / filename), record
