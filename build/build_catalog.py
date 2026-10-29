"""Build the trial-space catalog: small Markdown files a chat LLM can read by following links.

Source: MatchMiner-AI "trial spaces" (ksg-dfci/mmai-synthetic-1225, CC BY-NC 4.0), one row per space,
refreshed against the live ClinicalTrials.gov API so only trials still open to enrolment are kept.

    python build/build_catalog.py --spaces trial_space_lineitems.csv --cancer breast

The spaces CSV comes from
https://huggingface.co/datasets/ksg-dfci/mmai-synthetic-1225/resolve/main/trial_space_lineitems.csv (~785 MB).
Only the standard library and pandas are needed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import pandas as pd

RAW_BASE = "https://raw.githubusercontent.com/jim-bo/trial-finder/main/"
CTGOV_API = "https://clinicaltrials.gov/api/v2/studies"
OPEN_STATUSES = {"RECRUITING", "NOT_YET_RECRUITING"}
PART_SIZE = 80  # spaces per file: about 40 KB, comfortably inside one web fetch

FIELDS = [  # (label in MatchMiner-AI space text, label in our files)
    ("Age range allowed", "Age"),
    ("Sex allowed", "Sex"),
    ("Cancer type allowed", "Cancer type"),
    ("Histology allowed", "Histology"),
    ("Cancer burden allowed", "Disease extent"),
    ("Prior treatment required", "Prior treatment required"),
    ("Prior treatment excluded", "Prior treatment excluded"),
    ("Biomarkers required", "Biomarkers required"),
    ("Biomarkers excluded", "Biomarkers excluded"),
]

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def clean(text: str) -> str:
    """Normalise the unicode hyphens/spaces and markdown bold the source LLM emitted."""
    text = str(text)
    for a, b in (("‑", "-"), ("‐", "-"), ("–", "-"), ("−", "-"),
                 (" ", " "), ("\xa0", " "), ("**", "")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def parse_space(text: str) -> dict[str, str]:
    text = clean(text)
    out = {}
    for i, (src, dst) in enumerate(FIELDS):
        nxt = FIELDS[i + 1][0] + ":" if i + 1 < len(FIELDS) else None
        pat = re.escape(src) + r":\s*(.*?)" + (r"\s*" + re.escape(nxt) if nxt else r"\s*$")
        m = re.search(pat, text, re.S)
        value = m.group(1).strip().rstrip(".") if m else ""
        out[dst] = "" if value.upper() in {"", "NA", "N/A", "NONE", "NOT SPECIFIED"} else value
    return out


# --- classification -----------------------------------------------------------------------------------------

NEGATED_METS = re.compile(r"\b(no|absence of|without|free of|non|not)\b[^;.]{0,60}?(metasta|recurren)\w*"
                          r"(\s*(or|and|/)\s*(\w+\s+)?(metasta|recurren)\w*)?", re.I)
# early-disease phrases that contain an "advanced" word
NOT_ADVANCED = re.compile(r"risk of (\w+\s+){0,2}recurren\w*|(lymph[- ]?node|nodal|axillary|regional)\s+"
                          r"(\w+\s+)?metasta\w*|micro-?metasta\w*", re.I)
ADVANCED = re.compile(r"metasta|unresectable|inoperable|recurren|stage\s*iv|\bm1\b|relapsed|refractory|"
                      r"progress|advanced", re.I)
LOCAL_ONLY_ADVANCED = re.compile(r"locally[\s-]advanced", re.I)
EARLY = re.compile(r"early|stage\s*(0|i|ii|iii|1|2|3)\b|operable|resectable|neoadjuvant|adjuvant|locali[sz]ed|"
                   r"non-?metastatic|in situ|dcis|\bt[0-4]|\bn[0-3]\b|\bm0\b|newly diagnosed|high[- ]risk|"
                   r"locally[\s-]advanced|surgery|primary tumou?r", re.I)
BOTH = re.compile(r"any stage|all stages|early or (metastatic|advanced)|stage\s*(i|ii|iii|1|2|3)\s*(-|to|through)\s*"
                  r"(iv|4)|early[- ]stage or", re.I)


def settings(sp: dict[str, str]) -> list[str]:
    """'advanced', 'early' or 'any' (a space can land in both advanced and early)."""
    text = " ".join([sp["Cancer type"], sp["Disease extent"]])
    adv_text = LOCAL_ONLY_ADVANCED.sub(" ", NOT_ADVANCED.sub(" ", NEGATED_METS.sub(" ", text)))
    # treatment "for metastatic disease", required or excluded, also places a cohort in the advanced setting
    adv_text += " " + " ".join(re.findall(r"(?:for|in the)\s+(?:\w+\s+){0,3}?(?:metastatic|advanced|recurrent)",
                                          sp["Prior treatment required"] + " " + sp["Prior treatment excluded"],
                                          re.I))
    out = []
    if ADVANCED.search(adv_text):
        out.append("advanced")
        # an advanced cohort is also early only when it says so outright (e.g. "stage II-IV", "any stage")
        if BOTH.search(text):
            out.append("early")
    elif EARLY.search(text):
        out.append("early")
    return out or ["any"]


HER2 = r"(her[- ]?2|human epidermal growth factor receptor[- ]?2|erbb2)[\s-]*(\((her2|erbb2)\)\s*)?"
HER2_POS = re.compile(HER2 + r"(status\s*)?(positiv|\+|3\+|amplif|over-?express)", re.I)
HER2_NEG = re.compile(HER2 + r"(status\s*)?(negativ|-(?!\w)|low|ultra-?low|0|1\+|2\+)", re.I)
HR = (r"(\ber\b|\bpr\b|\bhr\b|\bpgr\b|estrogen|oestrogen|progesterone|hormone)([\s-]*receptors?)?"
      r"[\s-]*(\((er|pr|hr|pgr)\)\s*)?(expression\s*|staining\s*|status\s*)?")
HR_POS = re.compile(HR + r"(positiv|\+|(>|≥|greater than|at least)\s*[1-9]|-?rich)", re.I)
HR_NEG = re.compile(HR + r"(negativ|-(?!\w)|(<|≤|less than)\s*\d|low)", re.I)
TNBC = re.compile(r"triple[- ]negative|\btnbc\b", re.I)


def breast_subtypes(sp: dict[str, str]) -> list[str]:
    text = " ".join([sp["Cancer type"], sp["Histology"], sp["Biomarkers required"]])
    out = []
    if HER2_POS.search(text):
        out.append("her2-positive")
    if TNBC.search(text):
        out.append("triple-negative")
    if (HR_POS.search(text) or re.search(r"\besr1\b", text, re.I)) and not HER2_POS.search(text) \
            and "triple-negative" not in out:
        out.append("hr-positive-her2-negative")
    if not out and HER2_NEG.search(text):
        if HR_NEG.search(text):
            out.append("triple-negative")
        else:  # HER2-negative with hormone status open: HR+ and TNBC both fit
            out += ["hr-positive-her2-negative", "triple-negative"]
    return out or ["any-subtype"]


CANCERS = {
    "breast": {
        "title": "Breast cancer",
        "match": re.compile(r"breast", re.I),
        "exclude": re.compile(r"(exclud\w*|except|other than|not|non-?)\s+(\w+\s+){0,2}breast", re.I),
        "default_type": re.compile(r"(invasive |metastatic |advanced )?breast (cancer|carcinoma)", re.I),
        "subtypes": breast_subtypes,
        "subtype_titles": {
            "hr-positive-her2-negative": "HR+ / HER2-negative (includes HER2-low)",
            "her2-positive": "HER2-positive (includes HR+/HER2+)",
            "triple-negative": "Triple-negative",
            "any-subtype": "No subtype required (open to any breast cancer subtype)",
        },
    },
}

SETTING_TITLES = {
    "advanced": "Advanced: metastatic, unresectable or recurrent",
    "early": "Early: localized or locally advanced, around surgery (neoadjuvant/adjuvant)",
    "any": "Stage not specified (prevention, supportive care, imaging, any stage)",
}


# --- ClinicalTrials.gov refresh -----------------------------------------------------------------------------

def fetch_status(nct_ids: list[str], cache: Path) -> dict[str, dict]:
    """Current status, phase and site locations for each trial, cached as JSON."""
    got = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [n for n in nct_ids if n not in got]
    fields = ",".join(["NCTId", "BriefTitle", "OverallStatus", "Phase", "LocationCountry", "LocationState",
                       "LocationStatus", "LastUpdatePostDate"])
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        q = urllib.parse.urlencode({"filter.ids": ",".join(batch), "fields": fields, "pageSize": 100})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(f"{CTGOV_API}?{q}", timeout=60) as r:
                    studies = json.load(r)["studies"]
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        for s in studies:
            p = s["protocolSection"]
            locs = p.get("contactsLocationsModule", {}).get("locations", [])
            open_locs = [l for l in locs if l.get("status") in (None, "RECRUITING", "NOT_YET_RECRUITING")]
            got[p["identificationModule"]["nctId"]] = {
                "status": p["statusModule"]["overallStatus"],
                "updated": p["statusModule"].get("lastUpdatePostDateStruct", {}).get("date", ""),
                "phases": p.get("designModule", {}).get("phases", []),
                "title": p["identificationModule"].get("briefTitle", ""),
                "locations": [[l.get("country", ""), l.get("state", "")] for l in (open_locs or locs)],
            }
        for n in batch:
            got.setdefault(n, {"status": "NOT_FOUND"})
        cache.write_text(json.dumps(got))
        print(f"  ClinicalTrials.gov: {min(i + 100, len(todo))}/{len(todo)}", flush=True)
    return got


def where(locations: list[list[str]]) -> str:
    if not locations:
        return "sites not listed"
    by_country = defaultdict(set)
    counts = defaultdict(int)
    for country, state in locations:
        counts[country] += 1
        if country == "United States" and state:
            by_country[country].add(US_STATES.get(state, state))
    parts = []
    for country, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        label = "US" if country == "United States" else country
        if by_country[country]:
            parts.append(f"{label} ({', '.join(sorted(by_country[country]))})")
        else:
            parts.append(label)
    shown = parts[:8]
    more = f" +{len(parts) - 8} more countries" if len(parts) > 8 else ""
    return f"{len(locations)} site{'s' if len(locations) != 1 else ''}: " + "; ".join(shown) + more


def phase_label(phases: list[str]) -> str:
    if not phases or phases == ["NA"]:
        return "Phase n/a"
    return "Phase " + "/".join(p.replace("EARLY_PHASE1", "0").replace("PHASE", "") for p in phases)


# --- writing ------------------------------------------------------------------------------------------------

SHORT = {"Disease extent": "Extent", "Prior treatment required": "Prior tx required",
         "Prior treatment excluded": "Prior tx excluded"}


def render_trial(rows: list[dict], default_type: re.Pattern) -> str:
    """One trial: a header line, then one line per cohort; fields the trial leaves open are omitted."""
    r0 = rows[0]
    out = [f"**{r0['nct_id']}** {phase_label(r0['phases'])}, {r0['status'].replace('_', ' ').lower()}. "
           f"{r0['title']} [{r0['where']}]"]
    for row in rows:
        parts = []
        for _, label in FIELDS:
            value = row["space"][label]
            if not value or (label == "Cancer type" and default_type.fullmatch(value)):
                continue
            parts.append(f"{SHORT.get(label, label)}: {value}")
        out.append(f"- Cohort {row['space_number']} of {row['n_spaces']}: " + " | ".join(parts))
    return "\n".join(out)


def render(chunk: list[dict], default_type: re.Pattern) -> str:
    trials = defaultdict(list)
    for row in chunk:
        trials[row["nct_id"]].append(row)
    return "\n\n".join(render_trial(rows, default_type) for rows in trials.values())


def header(title: str, built: str) -> str:
    return (f"# {title}\n\n"
            f"Trial cohorts (\"spaces\") summarized by MatchMiner-AI from ClinicalTrials.gov, status checked "
            f"{built}. Each trial is followed by its cohorts; a cohort is one group a patient could join, "
            f"and every item listed for it must fit. A field that is not shown is not restricted. Trial page: https://clinicaltrials.gov/study/<NCT id>. Data: ksg-dfci/mmai-synthetic-1225 (CC BY-NC 4.0); "
            f"reformatted and filtered to open trials.\n")


def build(spaces_csv: Path, cancer: str, out_dir: Path, cache: Path) -> None:
    cfg = CANCERS[cancer]
    df = pd.read_csv(spaces_csv, usecols=["nct_id", "title", "this_space", "space_number"])
    df["n_spaces"] = df.groupby("nct_id")["nct_id"].transform("size")
    df["space"] = df["this_space"].map(parse_space)
    keep = df["space"].map(lambda s: bool(cfg["match"].search(s["Cancer type"]))
                           and not cfg["exclude"].search(s["Cancer type"]))
    df = df[keep].copy()
    print(f"{cancer}: {len(df)} spaces in {df.nct_id.nunique()} trials before the status check")

    status = fetch_status(sorted(df.nct_id.unique()), cache)
    built = dt.date.today().isoformat()
    rows = []
    for r in df.itertuples():
        st = status.get(r.nct_id, {})
        if st.get("status") not in OPEN_STATUSES:
            continue
        rows.append({"nct_id": r.nct_id, "title": clean(st.get("title") or r.title),
                     "space_number": r.space_number, "n_spaces": r.n_spaces, "space": r.space,
                     "status": st["status"], "phases": st.get("phases", []),
                     "where": where(st.get("locations", []))})
    print(f"{cancer}: {len(rows)} spaces in {len({r['nct_id'] for r in rows})} open trials")

    groups = defaultdict(list)
    for row in rows:
        for setting in settings(row["space"]):
            for sub in cfg["subtypes"](row["space"]):
                groups[(setting, sub)].append(row)

    cdir = out_dir / cancer
    if cdir.exists():
        for old in cdir.glob("*.md"):
            old.unlink()
    cdir.mkdir(parents=True, exist_ok=True)
    index_rows = []
    for setting in SETTING_TITLES:
        for sub in cfg["subtype_titles"]:
            items = sorted(groups.get((setting, sub), []), key=lambda r: (r["nct_id"], int(r["space_number"])))
            if not items:
                continue
            n_parts = (len(items) + PART_SIZE - 1) // PART_SIZE
            links = []
            for p in range(n_parts):
                name = f"{setting}--{sub}--{p + 1}.md"
                chunk = items[p * PART_SIZE:(p + 1) * PART_SIZE]
                title = (f"{cfg['title']} · {SETTING_TITLES[setting].split(':')[0]} · "
                         f"{cfg['subtype_titles'][sub]} · file {p + 1} of {n_parts}")
                body = header(title, built) + "\n" + render(chunk, cfg["default_type"]) + "\n"
                body += f"\n---\nEnd of file {p + 1} of {n_parts}."
                if p + 1 < n_parts:
                    body += f" Next: {RAW_BASE}catalog/{cancer}/{setting}--{sub}--{p + 2}.md"
                (cdir / name).write_text(body + "\n")
                links.append(f"{RAW_BASE}catalog/{cancer}/{name}")
            index_rows.append((setting, sub, len(items), len({r['nct_id'] for r in items}), links))

    lines = [f"# {cfg['title']}: catalog index", "",
             f"Status checked against ClinicalTrials.gov on {built}: {len(rows)} open cohorts in "
             f"{len({r['nct_id'] for r in rows})} recruiting or not-yet-recruiting trials. Cohorts that fit "
             f"more than one group appear in each.", "",
             "Read **every** file for the patient's stage group and subtype, **and** every file in the "
             "\"No subtype required\" group for the same stage. If the stage is unclear (for example "
             "locally advanced, or recurrence after surgery), read both the Advanced and Early groups.", ""]
    for setting in SETTING_TITLES:
        sel = [r for r in index_rows if r[0] == setting]
        if not sel:
            continue
        lines += [f"## {SETTING_TITLES[setting]}", ""]
        for _, sub, n, n_trials, links in sel:
            lines.append(f"**{cfg['subtype_titles'][sub]}**: {n} cohorts, {n_trials} trials, "
                         f"{len(links)} file{'s' if len(links) > 1 else ''}")
            lines += [f"- {u}" for u in links]
            lines.append("")
    (cdir / "index.md").write_text("\n".join(lines))
    print(f"wrote {sum(len(r[4]) for r in index_rows)} files to {cdir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spaces", type=Path, required=True, help="trial_space_lineitems.csv from mmai-synthetic-1225")
    ap.add_argument("--cancer", default="breast", choices=sorted(CANCERS))
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "catalog")
    ap.add_argument("--cache", type=Path, default=Path("ctgov_status_cache.json"))
    a = ap.parse_args()
    build(a.spaces, a.cancer, a.out, a.cache)


if __name__ == "__main__":
    main()
