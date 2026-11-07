#!/usr/bin/env python3
"""Render results/runs.tsv into the run tables of results.md.

Replaces the text between <!-- runs:begin --> and <!-- runs:end --> (all runs)
and between <!-- totals:begin --> and <!-- totals:end --> (valid run-hours per
stage). Hand-written annotations in runs.tsv are kept as they are.
"""
import csv
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TSV = os.path.join(ROOT, "results", "runs.tsv")
MD = os.path.join(ROOT, "results.md")


def kind(row):
    o = row["outcome"]
    label = row["run_id"]
    if "POSITIVE CONTROL" in o:
        return "control"
    if o.startswith("INVALID") or "INVALID" in o[:40]:
        return "invalid"
    if any(t in label for t in ("tune", "smoke", "faultcheck")):
        return "tuning"
    return "valid"


def seconds(row):
    m = re.search(r"\d+", row["duration_s"])
    return int(m.group()) if m else 0


def main():
    with open(TSV) as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    lines = ["| run | stage | kernel | faults | workload | duration | class | outcome |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        k = kind(r)
        out = r["outcome"].replace("|", "/")
        lines.append("| `%s` | %s | %s | %s | `%s` | %s s | %s | %s |" % (
            r["run_id"], r["stage"], r["kernel"].replace(".x86_64", ""), r["faults"], r["workload_args"],
            r["duration_s"], k, out))
    runs = "\n".join(lines)

    tot = {}
    for r in rows:
        if kind(r) != "valid":
            continue
        key = (r["stage"], r["kernel"].replace(".x86_64", ""), "none" if r["faults"] in ("none", "") else "faults")
        t = tot.setdefault(key, [0, 0, 0])
        t[0] += 1
        t[1] += seconds(r)
        t[2] += 1 if r["outcome"].startswith(("REPRODUCED", "LEAK")) else 0
    tl = ["| stage | kernel | faults | valid runs | total duration | runs with a persistent stall/leak |",
          "|---|---|---|---|---|---|"]
    for (st, kern, fl), (n, s, rep) in sorted(tot.items()):
        tl.append("| %s | %s | %s | %d | %.1f h | %d |" % (st, kern, fl, n, s / 3600, rep))
    totals = "\n".join(tl)

    md = open(MD).read()
    for tag, body in (("runs", runs), ("totals", totals)):
        md = re.sub(r"(<!-- %s:begin -->).*?(<!-- %s:end -->)" % (tag, tag),
                    lambda m: m.group(1) + "\n" + body + "\n" + m.group(2), md, flags=re.S)
    open(MD, "w").write(md)
    print("updated %s: %d runs" % (MD, len(rows)))


if __name__ == "__main__":
    main()
