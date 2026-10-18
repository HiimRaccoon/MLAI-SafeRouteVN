"""Resumable bulk PBF download: Range/If-Range, provider MD5, local SHA-256."""

import hashlib
import os
from pathlib import Path
import re
import time

import requests

from geo_data.common import exclusive_lock, now_vn, read_json, write_json

DEFAULT_EXTRACT_URL = "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf"


def file_hash(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download_extract(output, *, url=DEFAULT_EXTRACT_URL, user_agent, attempts=5,
                     session=None, sleep=time.sleep, progress=print):
    if not url.startswith("https://") or not url.endswith(".osm.pbf"):
        raise ValueError("Expected an HTTPS .osm.pbf URL with an accompanying .md5 file")
    if not user_agent.strip() or not 1 <= attempts <= 10:
        raise ValueError("User-Agent required; attempts must be 1..10")
    session = session or requests.Session()
    output = Path(output)
    final = output / "source.osm.pbf"
    partial = output / "source.osm.pbf.part"
    state_file = output / "download-state.json"
    manifest_file = output / "extract-manifest.json"
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    with exclusive_lock(output):
        if final.exists() and manifest_file.exists():
            manifest = read_json(manifest_file)
            if manifest.get("url") != url:
                raise ValueError("Output has another source URL; use a new output directory")
            if final.stat().st_size != manifest.get("bytes") or file_hash(final) != manifest.get("sha256"):
                raise ValueError("Completed extract checksum mismatch; use a new output directory")
            progress(f"PBF cache verified: {final} ({manifest['bytes'] / 1e6:.1f} MB)")
            return manifest
        if final.exists():
            raise ValueError("PBF exists without a manifest; inspect it or choose a new output directory")
        if state_file.exists() and read_json(state_file).get("url") != url:
            raise ValueError("Partial download belongs to another URL; use a new output directory")
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                with session.get(url + ".md5", headers=headers, timeout=(15, 30)) as checksum_response:
                    checksum_response.raise_for_status()
                    checksum_match = re.match(r"^([0-9a-fA-F]{32})(?:\s|$)", checksum_response.text.strip())
                    if not checksum_match:
                        raise ValueError("Provider .md5 response is invalid")
                    expected_md5 = checksum_match[1].lower()
                with session.head(url, headers=headers, allow_redirects=True, timeout=(15, 30)) as head:
                    head.raise_for_status()
                    total = int(head.headers.get("Content-Length", "0"))
                    if total <= 0:
                        raise ValueError("Source must provide Content-Length for verified resume")
                    etag = head.headers.get("ETag")
                    validator = etag if etag and not etag.startswith("W/") else head.headers.get("Last-Modified")
                    identity = {"url": url, "resolvedUrl": head.url, "bytes": total,
                                "md5": expected_md5, "validator": validator}
                previous = read_json(state_file) if state_file.exists() else None
                if partial.exists() and (previous != identity or not validator or partial.stat().st_size > total):
                    progress("Source changed or resume is unverifiable; restarting this partial download.")
                    partial.unlink()
                write_json(state_file, identity)
                offset = partial.stat().st_size if partial.exists() else 0
                progress(f"PBF attempt {attempt}/{attempts}: {offset / 1e6:.1f}/{total / 1e6:.1f} MB ({offset / total:.1%})")
                if offset < total:
                    request_headers = dict(headers)
                    if offset:
                        request_headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
                    with session.get(url, headers=request_headers, stream=True, timeout=(15, 60)) as response:
                        response.raise_for_status()
                        if response.headers.get("Content-Encoding", "identity") != "identity":
                            raise ValueError("Unexpected transfer encoding; cannot safely resume")
                        if response.status_code == 206:
                            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                            if not match or int(match[1]) != offset or int(match[3]) != total or int(match[2]) != total - 1:
                                raise ValueError("Unexpected Content-Range; partial file was not appended")
                            mode = "ab"
                        elif response.status_code == 200:
                            mode, offset = "wb", 0  # Server ignored Range or If-Range no longer matched.
                        else:
                            raise ValueError(f"Unexpected download HTTP status: {response.status_code}")
                        started, last_log, initial = time.monotonic(), time.monotonic(), offset
                        with partial.open(mode) as stream:
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if not chunk:
                                    continue
                                stream.write(chunk)
                                offset += len(chunk)
                                if offset > total:
                                    raise ValueError("Download exceeded expected size; source may have changed")
                                current = time.monotonic()
                                if current - last_log >= 2 or offset == total:
                                    rate = (offset - initial) / max(current - started, 0.001) / 1e6
                                    progress(f"PBF {offset / 1e6:.1f}/{total / 1e6:.1f} MB ({offset / total:.1%}) | {rate:.1f} MB/s")
                                    last_log = current
                            stream.flush()
                            os.fsync(stream.fileno())
                if not partial.exists() or partial.stat().st_size != total:
                    raise requests.ConnectionError("Transfer ended before Content-Length; retaining partial file")
                progress("Verifying provider MD5 and local SHA-256...")
                if file_hash(partial, "md5") != expected_md5:
                    partial.unlink()  # Known invalid, do not present it as a resumable valid prefix.
                    raise requests.ConnectionError("MD5 mismatch; discarded corrupt bytes, retrying source")
                sha256 = file_hash(partial)
                manifest = {"schemaVersion": "member1-extract/1", **identity, "sha256": sha256,
                            "file": final.name, "fetchedAt": now_vn(), "complete": True,
                            "sourceType": "REAL-derived", "routingReady": False,
                            "attribution": "© OpenStreetMap contributors, Geofabrik extract, ODbL 1.0"}
                # Manifest first permits recovery if interrupted immediately before the rename.
                write_json(manifest_file, manifest)
                os.replace(partial, final)
                progress(f"PBF complete and verified: {final}")
                return manifest
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                progress(f"PBF attempt {attempt}/{attempts} failed: {exc}")
                status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
                if status is not None and status not in (408, 429, 500, 502, 503, 504):
                    raise
                if isinstance(exc, ValueError):
                    raise
                if attempt < attempts:
                    sleep(min(5 * 2 ** (attempt - 1), 30))
        raise requests.ConnectionError(f"Extract download interrupted after {attempts} attempts. Rerun to resume. {last_error}")
