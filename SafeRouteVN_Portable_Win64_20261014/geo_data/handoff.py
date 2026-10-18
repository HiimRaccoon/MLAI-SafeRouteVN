"""Portable Member 1 handoff folders with pinned data, SDK and QA lineage."""

import os
from pathlib import Path
import shutil

from geo_data.bundle import cached, publish, temporary, verify
from geo_data.common import digest, exclusive_lock, read_json, write_json
from geo_data.osm.extract_download import file_hash

HANDOFF_SCHEMA = "member1-handoff/1"
STAGE_SCHEMAS = {
    "routing": "member1-routing/1", "travel": "member1-travel/1",
    "weather-plan": "member1-weather-plan/1", "weather": "member1-weather-context/1",
    "features": "member1-edge-features/1", "scenarios": "member1-scenarios/1", "qa": "member1-qa/1",
}


def verify_run(run):
    run = Path(run)
    manifests = {stage: verify(run / stage, schema) for stage, schema in STAGE_SCHEMAS.items()}
    qa = manifests["qa"]
    report = read_json(run / "qa/qa_report.json")
    if qa.get("checksPassed") is not True or report.get("checksPassed") is not True:
        raise ValueError("Handoff requires passing Member 1 QA")
    for stage, manifest in manifests.items():
        if stage == "qa":
            continue
        key = "plan" if stage == "weather-plan" else stage
        expected = {"version": manifest["version"], "files": manifest["files"]}
        if qa["identity"]["inputs"].get(key) != expected:
            raise ValueError(f"QA was run against a different {stage} bundle")
    scenarios = manifests["scenarios"]
    if scenarios.get("suiteReady") is not True or not all(scenarios.get(k) is True for k in ("s7Verified", "s8Verified")):
        raise ValueError("S7/S8 evidence is incomplete; finish scenario acceptance before packaging")
    if not all(qa.get(k) is True and report.get(k) is True for k in ("suiteReady", "s7Verified", "s8Verified")):
        raise ValueError("QA and scenario acceptance disagree")
    return manifests


def disjoint(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Output must be separate from the input folder (not inside it or its ancestor)")


def sdk_inventory():
    root = Path(__file__).resolve().parent
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(p in ("tests", "__pycache__") for p in relative.parts):
            continue
        if path.suffix in (".py", ".json") or path.name in ("requirements.txt", "requirements.lock.txt"):
            if not path.resolve().is_relative_to(root):
                raise ValueError("SDK input escapes the geo_data directory")
            files["sdk/geo_data/" + relative.as_posix()] = path
    return files


def copy_verified(source, target, expected):
    """Resume at file granularity. Never append to an unknown partial copy."""
    if target.exists():
        if file_hash(target) == expected:
            return "cached"
        raise ValueError(f"Existing handoff file has a different checksum: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    with Path(source).open("rb") as src, part.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    if file_hash(part) != expected:
        raise ValueError(f"Source changed during handoff copy: {source}")
    os.replace(part, target)
    return "copied"


def build_handoff(run, output, *, review=None, graph=None, progress=print):
    run, output = Path(run), Path(output)
    disjoint(run, output)
    disjoint(Path(__file__).parent, output)
    progress("Handoff [1/3] verifying stage checksums and exact QA inputs...")
    manifests = verify_run(run)
    inventory = {}
    settings = read_json(run / "run-settings.json") if (run / "run-settings.json").exists() else {}
    graph_path = graph or settings.get("graph")
    if not graph_path:
        raise ValueError("Supply --graph to include the original access policy provenance (run-settings.json is absent)")
    graph_path = Path(graph_path)
    disjoint(graph_path, output)
    graph_manifest = read_json(graph_path / "manifest.json")
    net = manifests["routing"]
    if (graph_manifest.get("graphVersion") != net["graphVersion"] or
            graph_manifest.get("files") != net["identity"]["sourceFiles"]):
        raise ValueError("Graph provenance does not match the routing input")
    inventory["provenance/graph-manifest.json"] = (graph_path / "manifest.json", file_hash(graph_path / "manifest.json"))
    for name in ("policy.json", "qa_report.json"):
        path = graph_path / name
        expected = graph_manifest["files"][name]["sha256"]
        if file_hash(path) != expected:
            raise ValueError(f"Graph provenance checksum mismatch: {name}")
        inventory["provenance/graph-" + name] = (path, expected)
    for stage, manifest in manifests.items():
        source_manifest = run / stage / "manifest.json"
        inventory[f"data/{stage}/manifest.json"] = (source_manifest, file_hash(source_manifest))
        for name, meta in manifest["files"].items():
            inventory[f"data/{stage}/{name}"] = (run / stage / name, meta["sha256"])
    for name, path in sdk_inventory().items():
        inventory[name] = (path, file_hash(path))
    review_version = None
    if review:
        review = Path(review)
        disjoint(review, output)
        checked = verify(review, "member1-quality-review/1")
        if checked["identity"]["inputs"] != {k: digest(v) for k, v in manifests.items()}:
            raise ValueError("Quality review belongs to another run")
        review_version = checked["version"]
        inventory["review/manifest.json"] = (review / "manifest.json", file_hash(review / "manifest.json"))
        for name, meta in checked["files"].items():
            inventory["review/" + name] = (review / name, meta["sha256"])
    docs = Path(__file__).parent.parent / "docs"
    for name in ("member1_runbook.md",):
        path = docs / name
        if path.is_file():
            inventory["docs/" + name] = (path, file_hash(path))
    identity = {"stage": HANDOFF_SCHEMA, "inventory": {k: v[1] for k, v in sorted(inventory.items())},
                "reviewVersion": review_version, "readmeVersion": 2}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Handoff cache verified: {output}")
            return saved
        progress(f"Handoff [2/3] copying {len(inventory)} files; completed files are reusable on restart...")
        for index, (name, (source, checksum)) in enumerate(sorted(inventory.items()), 1):
            result = copy_verified(source, output / name, checksum)
            progress(f"  [{index}/{len(inventory)}] {name}: {result}")
        readme = """# SafeRoute VN — Member 1 handoff

This folder contains verified stage data, a frozen Python SDK and schema notes.
Data paths are relative; original absolute paths in provenance are informational.
Use Python 3.12 and install dependencies before offline use:

```powershell
cd sdk
python -m pip install -r geo_data/requirements.lock.txt
python -m geo_data.cli verify-handoff --package ..
python -m geo_data.cli consumer-smoke --package .. --output ../../member1-consumer-smoke
```

`data/` contains the seven QA-checked stages. `sdk/geo_data/` contains the exact
reader code copied at packaging time. [Member 1 guide](docs/member1_runbook.md)
contains the data contract, handoff instructions and operational commands.
`review/`, when included, contains computed diagnostics and map samples to inspect.
No country PBF, old tile cache or preview graph is needed by the reader.

Routing requires forbidden-turn checks and incoming-edge state across route legs.
Travel is ESTIMATED, safety is an uncalibrated PROXY, scenarios are SYNTHETIC.
Weather is one frozen model-data snapshot. This is not live traffic.
Consumer smoke checks data access and recorded S0 paths; it is not a VRP solve,
backend replay, field audit or independent validation of source permissions.
Member 2/3 integration and manual policy/boundary/profile review remain pending.

Attribution: © OpenStreetMap contributors, ODbL 1.0; weather by Open-Meteo, CC BY 4.0.
Retain provenance, attribution and applicable source terms when sharing derivatives.
"""
        temporary(output, "README.md").write_text(readme, encoding="utf-8")
        acceptance = {"member1QaPassed": True, "scenarioSuiteReady": True,
                      "consumerSmokePassed": None, "member2Integration": "pending", "member3Integration": "pending",
                      "manualBoundaryReview": "pending", "manualAccessReview": "pending", "profileCalibration": "pending",
                      "integrated": False, "note": "Template only. Store review evidence separately; do not edit a checksummed package."}
        write_json(temporary(output, "acceptance_template.json"), acceptance)
        progress("Handoff [3/3] publishing package manifest...")
        return publish(output, identity, HANDOFF_SCHEMA, ["README.md", "acceptance_template.json"],
                       retained=list(inventory), at=manifests["features"]["at"],
                       versions={k: m["version"] for k, m in manifests.items()},
                       member1QaPassed=True, suiteReady=True, integrated=False,
                       requiresTurnAwareReader=True, reviewIncluded=review is not None,
                       sourceRun=str(run.resolve()), bytesCopied=sum((output / name).stat().st_size for name in inventory))


def verify_handoff(package):
    package = Path(package)
    manifest = verify(package, HANDOFF_SCHEMA)
    for name, expected in manifest["identity"]["inventory"].items():
        if manifest["files"].get(name, {}).get("sha256") != expected:
            raise ValueError("Handoff inventory differs from its frozen identity")
    # Stage manifests must themselves be protected by the outer package checksum.
    required = {"README.md", "acceptance_template.json", "sdk/geo_data/consumer.py", "sdk/geo_data/osm/routing.py",
                "provenance/graph-manifest.json", "provenance/graph-policy.json", "provenance/graph-qa_report.json"}
    for stage in STAGE_SCHEMAS:
        required.add(f"data/{stage}/manifest.json")
        stage_manifest = read_json(package / "data" / stage / "manifest.json")
        required.update(f"data/{stage}/{name}" for name in stage_manifest["files"])
    if not required.issubset(manifest["files"]):
        raise ValueError("Incomplete handoff inventory")
    stages = verify_run(package / "data")
    if manifest["versions"] != {k: m["version"] for k, m in stages.items()}:
        raise ValueError("Handoff version bindings differ from packaged stages")
    provenance = read_json(package / "provenance/graph-manifest.json")
    if (provenance["graphVersion"] != stages["routing"]["graphVersion"] or
            provenance["files"] != stages["routing"]["identity"]["sourceFiles"]):
        raise ValueError("Packaged graph provenance does not match routing")
    for name in ("policy.json", "qa_report.json"):
        if file_hash(package / "provenance" / ("graph-" + name)) != provenance["files"][name]["sha256"]:
            raise ValueError("Packaged graph policy/report provenance checksum failed")
    return manifest
