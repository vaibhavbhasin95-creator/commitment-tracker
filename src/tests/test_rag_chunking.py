"""RAG chunking (rag/chunking.py, Step 1.2): size cap, no lost turns, one-turn overlap,
oversized turns, tiny tails and odd inputs. Fast: no database, no model, no network."""

import dataclasses

from _support import ROOT, ok

import transcript_parser as tp
from rag.chunking import MAX_CHARS, TARGET_CHARS, chunk_transcript


def dialogue(count, words_per_turn=20, speakers=("Person A", "Person B", "Person C")):
    """`count` short turns in 'Name: text' form; every turn's text is unique."""
    return "\n".join(
        f"{speakers[i % len(speakers)]}: " + " ".join(f"word{i}x{j}" for j in range(words_per_turn)) + "."
        for i in range(count)
    )


def turns_covered(chunks):
    seen = set()
    for c in chunks:
        seen.update(range(c.turn_start, c.turn_end + 1))
    return seen


def all_within_cap(chunks, cap=MAX_CHARS):
    return all(len(c.text) <= cap for c in chunks)


# --- empty and tiny inputs ---------------------------------------------------
ok(chunk_transcript("") == [] and chunk_transcript("   \n\n  ") == [], "empty or blank transcript -> no chunks")

tiny = chunk_transcript("Person A: Hello.\nPerson B: Hi.")
ok(len(tiny) == 1 and tiny[0].text == "Person A: Hello.\nPerson B: Hi.", "two short turns -> one chunk with 'Speaker: words' lines")
ok(tiny[0].speakers == ("Person A", "Person B") and (tiny[0].turn_start, tiny[0].turn_end) == (0, 1),
   "chunk records its speakers in order and its turn range")

# --- normal dialogue -----------------------------------------------------------
raw = dialogue(80)
chunks = chunk_transcript(raw)
ok(len(chunks) > 1, "a long dialogue is split into several chunks")
ok(all_within_cap(chunks), f"no chunk is longer than {MAX_CHARS} characters")
ok(turns_covered(chunks) == set(range(len(tp.to_turns(raw)))), "every turn appears in at least one chunk")
ok([c.turn_start for c in chunks] == sorted(c.turn_start for c in chunks), "chunks come out in transcript order")
ok(all(b.turn_start == a.turn_end for a, b in zip(chunks, chunks[1:])), "each chunk starts with the previous chunk's last turn")
ok(all(len(set(c.speakers)) == len(c.speakers) for c in chunks), "a chunk lists each speaker once")
ok(all(len(c.text) >= 1 for c in chunks), "no empty chunks")
ok(chunks == chunk_transcript(raw), "same input -> identical chunks (deterministic)")
ok(len(chunk_transcript(raw, target_chars=400, max_chars=600)) > len(chunks), "a smaller target gives more chunks")
ok(all_within_cap(chunk_transcript(raw, target_chars=400, max_chars=600), 600), "a custom max_chars is respected")

# --- overlap is skipped when it wouldn't fit ---------------------------------
big = "\n".join(f"Person {'AB'[i % 2]}: " + ("long " * 140).strip() + f" turn{i}." for i in range(4))  # about 700 characters each
big_chunks = chunk_transcript(big)
ok(all_within_cap(big_chunks), "overlap never pushes a chunk over the cap")
ok(not any(b.turn_start == a.turn_end for a, b in zip(big_chunks, big_chunks[1:])), "overlap is dropped when two turns together wouldn't fit")
ok(turns_covered(big_chunks) == {0, 1, 2, 3}, "turns are still all covered without overlap")

# --- oversized turns ----------------------------------------------------------
sentences = [f"Sentence number {i} is part of the long update." for i in range(150)]
long_turn = chunk_transcript("Person A: " + " ".join(sentences))
ok(len(long_turn) > 1 and all_within_cap(long_turn), "one huge turn is split into chunks under the cap")
ok(all(c.speakers == ("Person A",) and (c.turn_start, c.turn_end) == (0, 0) for c in long_turn), "pieces of one turn keep its speaker and turn index")
ok(all(any(s in c.text for c in long_turn) for s in sentences), "no sentence is lost when a turn is split")
ok(all(not c.text.split(": ", 1)[1].startswith(" ") for c in long_turn), "split pieces start cleanly")

no_ends = " ".join(f"tok{i}" for i in range(900))  # no sentence punctuation at all
word_split = chunk_transcript("Person A: " + no_ends)
ok(len(word_split) > 1 and all_within_cap(word_split), "a turn with no sentence ends is split on words")
ok(all(any(f"tok{i}" in c.text.split() or f"tok{i}\n" in c.text for c in word_split) for i in range(0, 900, 50)), "no words are lost in a word split")

giant = chunk_transcript("Person A: " + "x" * 3000)
ok(len(giant) > 1 and all_within_cap(giant), "one 3,000-character 'word' is cut under the cap")
ok(sum(c.text.count("x") for c in giant) >= 3000, "none of a giant word's characters is lost")

# --- the tail ------------------------------------------------------------------
# Each Ann line is 295 characters, so "Bob: Thanks." (12) just misses the 300 target and would
# otherwise become a chunk of its own; it fits under the cap, so it is merged instead.
tail = chunk_transcript("\n".join([f"Ann: {'x' * 290}"] * 3 + ["Bob: Thanks."]), target_chars=300)
ok(len(tail) == 3 and tail[-1].turn_end == 3, "a tiny last turn is merged into the chunk before it")
ok("Bob: Thanks." in tail[-1].text and "Ann:" in tail[-1].text, "the merged tail keeps its context")
ok(all(c.text != "Bob: Thanks." for c in tail), "no chunk is just a tiny tail")

# --- odd inputs -------------------------------------------------------------------
unlabelled = chunk_transcript("This is plain text with no speakers at all. " * 60)
ok(len(unlabelled) > 1 and all_within_cap(unlabelled), "text with no speaker labels is still chunked")
ok(all(c.speakers == ("Unknown",) for c in unlabelled), "no speaker label is shown as 'Unknown'")

many = chunk_transcript("\n".join(f"{n}: Yes." for n in ["Ann", "Bob"] * 150))
ok(len(many) < 20 and all_within_cap(many), "hundreds of one-word turns group into a few chunks")

timestamped = chunk_transcript("[09:00] Maya Singh: Good morning.\n[09:02] Arjun Mehta: Hi there.")
ok(timestamped[0].speakers == ("Maya Singh", "Arjun Mehta"), "speaker turns come from the app's own transcript parser")

try:
    chunk_transcript("Person A: x", target_chars=500, max_chars=100)
    ok(False, "target_chars above max_chars is refused")
except ValueError:
    ok(True, "target_chars above max_chars is refused")

try:
    tiny[0].text = "changed"
    ok(False, "a Chunk can't be changed after it is made")
except dataclasses.FrozenInstanceError:
    ok(True, "a Chunk can't be changed after it is made")

# --- a real sample transcript ----------------------------------------------------
sample = ROOT.parent / "synthetic_meeting_transcripts_txt" / "01_ai_customer_service.txt"
real = chunk_transcript(sample.read_text(encoding="utf-8"))
ok(len(real) > 5 and all_within_cap(real), "sample meeting transcript: several chunks, all under the cap")
ok(turns_covered(real) == set(range(len(tp.to_turns(sample.read_text(encoding='utf-8'))))), "sample meeting transcript: every turn covered")
ok(TARGET_CHARS < MAX_CHARS <= 1300, "default sizes stay under the embedding model's ~1,300-character reading limit")
