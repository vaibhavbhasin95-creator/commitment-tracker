"""
Turns the team's meeting transcripts into utterances ready for labelling.

Each transcript is split exactly the way the app splits an uploaded transcript
(transcript_parser.to_turns -> to_utterances), so the model trains on the same
kind of text it will see at inference time.

Files are grouped into *families*: files generated from the same template, or
the same source, belong together. Evaluation always holds out a whole family,
so the model is never tested on near-copies of what it trained on.

Near-duplicates are collapsed: participant names are masked ("<P>") and each
distinct masked sentence is kept once per family with a count of how often it
occurred. (The 180-meeting corpus is ~48k dialogue lines but only 273 distinct
sentences — training on every copy would just teach those 273 sentences.)

Output: training_data/utterances_for_labelling.csv
    item_id, family, sources, occurrences, speaker, text, masked_text

Run:  python ml/prepare_transcript_dataset.py
"""

import csv
import re
import sys
import zipfile
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import transcript_parser as tp  # noqa: E402

SRC = ROOT / "synthetic_meeting_transcripts_txt"
OUT = ROOT / "training_data" / "utterances_for_labelling.csv"


def docx_text(path):
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    paras = [re.sub(r"<[^>]+>", "", p) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
    unescape = lambda s: s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
    return "\n".join(unescape(p).strip() for p in paras if p.strip())


def reflowed_vtt_text(path):
    """The lecture file is a WebVTT transcript whose line breaks were lost
    (re-wrapped at ~70 chars, '-->' damaged). Rebuild one 'Speaker: words'
    line per cue; text before the first cue is written notes."""
    body = re.sub(r"\s+", " ", path.read_text(encoding="utf-8", errors="replace"))
    cue = re.compile(r"\b\d+ \d{2}:\d{2}:\d{2}\.\d{3} \S{1,3} \d{2}:\d{2}:\d{2}\.\d{3} ")
    parts = cue.split(body)
    preamble, cues = parts[0], parts[1:]
    lines = ["WRITTEN NOTES", preamble.strip()]
    lines += [c.strip() for c in cues if c.strip()]
    return "\n".join(lines)


def split_corpus(path):
    """The 180-meeting corpus: one text block per meeting."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return [m for m in re.split(r"\n#{20,}\nMEETING \d+:[^\n]*\n#{20,}\n", text)[1:]]


def sources():
    """(family, source_name, text) for every transcript."""
    for f in sorted(SRC.glob("[0-9][0-9]_*.txt")):
        yield "template_numbered", f.name, f.read_text(encoding="utf-8")
    for i, meeting in enumerate(split_corpus(SRC / "large_synthetic_meeting_transcript.txt"), 1):
        yield "template_corpus", f"large_synthetic#{i:03d}", meeting
    yield "workshop_clustering", "session4_transformed_long_transcript.docx", docx_text(SRC / "session4_transformed_long_transcript.docx")
    yield "workshop_validation", "transformed_meeting_transcript.docx", docx_text(SRC / "transformed_meeting_transcript.docx")
    yield "real_lecture", "fraud_ai_implementation_meeting_transcript.txt", reflowed_vtt_text(SRC / "fraud_ai_implementation_meeting_transcript.txt")
    yield "fy2027", "FY2027_Financial_Planning_Meeting_Transcript.txt", (ROOT / "FY2027_Financial_Planning_Meeting_Transcript.txt").read_text(encoding="utf-8-sig")


def mask_names(text, names):
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(name)}\b", "<P>", text)
    return text


def main():
    items = OrderedDict()  # (family, masked_text) -> row
    for family, source, text in sources():
        turns = tp.to_turns(text)
        if turns.empty:
            continue
        names = {s for s in turns.speaker if s != "UNKNOWN"}
        names |= {part for s in names for part in s.split() if len(part) > 2}
        # de-duplicate whole turns before the (slow) sentence splitter
        seen_turns = {}
        for t in turns.itertuples():
            key = (t.speaker if t.speaker == "UNKNOWN" else "<P>", mask_names(t.text, names))
            seen_turns.setdefault(key, t)
        unique_turns = turns.loc[[t.Index for t in seen_turns.values()]]
        for u in tp.to_utterances(unique_turns).itertuples():
            masked = mask_names(u.text, names)
            row = items.get((family, masked))
            if row is None:
                items[(family, masked)] = {"family": family, "sources": {source}, "occurrences": 1,
                                           "speaker": u.speaker, "text": u.text, "masked_text": masked}
            else:
                row["occurrences"] += 1
                row["sources"].add(source)
        print(f"  {family:22s} {source}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "family", "sources", "occurrences", "speaker", "text", "masked_text"])
        for i, row in enumerate(items.values(), 1):
            src = sorted(row["sources"])
            w.writerow([i, row["family"], src[0] if len(src) == 1 else f"{len(src)} files", row["occurrences"],
                        row["speaker"], row["text"], row["masked_text"]])
    counts = {}
    for row in items.values():
        counts[row["family"]] = counts.get(row["family"], 0) + 1
    print(f"\n{len(items)} distinct utterances -> {OUT}")
    for fam, n in counts.items():
        print(f"  {fam:22s} {n}")


if __name__ == "__main__":
    main()
