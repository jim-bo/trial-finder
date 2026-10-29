# Trial Finder

Find cancer clinical trials worth asking your oncologist about, using an ordinary Claude or Gemini chat. There's nothing to install.

**Pilot: breast cancer only.**

## How to use it

Open a new chat in Claude (claude.ai or the app) or Gemini and paste:

> Please read https://raw.githubusercontent.com/jim-bo/trial-finder/main/start.md and follow its instructions to help me find breast cancer clinical trials.

The assistant then:
1. helps you write a short summary of your situation, from documents you upload or by asking you questions;
2. reads the trial files that fit your cancer subtype and stage;
3. returns a ranked shortlist with links to ClinicalTrials.gov, the reasons each trial may fit, what still needs checking, and questions for your care team.

The assistant needs web access to read the files. In Claude this means web search must be turned on (in the chat's tools menu or in settings). If it can't open links, download the files instead: `start.md` plus your files from `catalog/breast/`. Attach them to the chat with the same request.

Don't upload your name, date of birth, address or record numbers. Black them out on documents first.

**This is not medical advice.** It suggests trials to discuss with your care team. Only the trial team can decide whether you are eligible.

## What's inside

- `start.md`: instructions the assistant follows.
- `catalog/breast/index.md`: lists the catalog files by stage (advanced, early, not specified) and subtype (HR+/HER2−, HER2+, triple-negative, any).
- `catalog/breast/*.md`: one entry per trial cohort, giving age, sex, histology, disease extent, required or excluded prior treatment, required or excluded biomarkers, phase, status and site locations.
- `build/build_catalog.py`: rebuilds the catalog.

## Where the data comes from

Trial cohorts ("trial spaces") were extracted from ClinicalTrials.gov eligibility criteria by [MatchMiner-AI](https://arxiv.org/abs/2412.17228) (Dana-Farber Cancer Institute) and released in [`ksg-dfci/mmai-synthetic-1225`](https://huggingface.co/datasets/ksg-dfci/mmai-synthetic-1225) under **CC BY-NC 4.0**. That release covers cancer trials that were recruiting or not yet recruiting on 3 November 2025. This repository reformats those spaces and drops trials that are no longer recruiting according to the live ClinicalTrials.gov API (the check date is in each file). Trials registered after November 2025 are not included. Non-commercial use only.

## Rebuilding

```bash
curl -LO https://huggingface.co/datasets/ksg-dfci/mmai-synthetic-1225/resolve/main/trial_space_lineitems.csv   # ~785 MB
pip install pandas
python build/build_catalog.py --spaces trial_space_lineitems.csv --cancer breast
```
