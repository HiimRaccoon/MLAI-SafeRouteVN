"""Run from project root (or packaged sdk/): python -m geo_data.examples.read_handoff --package PATH."""

import argparse
import json

from geo_data.consumer import Member1Dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    args = parser.parse_args()
    with Member1Dataset(args.package) as data:
        scenario = data.scenario("S0")
        print(json.dumps({"scenarioId": scenario["scenarioId"], "at": scenario["initialState"]["currentTime"],
                          "orders": len(scenario["initialState"]["orders"]), "vehicles": len(scenario["initialState"]["vehicles"]),
                          "featuresVersion": scenario["featuresVersion"], "integrated": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
