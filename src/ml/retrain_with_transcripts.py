"""
Retrains the commitment classifier with the team's labelled transcripts and
measures it honestly.

Data
  * the original synthetic CSV (train_utterances_balanced.csv, 44.5k rows)
  * training_data/labelled_utterances.csv — distinct utterances from the team's
    transcripts, grouped into families (see ml/prepare_transcript_dataset.py)

Evaluation — leave-one-family-out
  For each family F: train on the CSV + every other family, choose the decision
  threshold on a validation slice of the *training* families, then score on F,
  which the model never saw. The current production model is scored on F too.
  Families group near-copies together, so this never tests on a disguised copy
  of a training example.

Final model
  Trained on the CSV + all families; threshold = median of the per-fold
  thresholds. It is written to ml/action_detector.keras ONLY if it beats the
  current model on the leave-one-family-out macro-F1; otherwise it's saved
  alongside as ml/action_detector_candidate.keras for inspection.

Output: ml/retrain_report.json (all numbers) and a printed summary.

Run:  python src/ml/retrain_with_transcripts.py
"""

import csv
import json
import os
import random
from datetime import date
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import tensorflow as tf  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]   # project root (this script lives in src/ml/)
ML = ROOT / "ml"                             # model artifacts
CSV_TRAIN = ROOT / "train_utterances_balanced.csv"
LABELLED = ROOT / "training_data" / "labelled_utterances.csv"
PROD_MODEL = ML / "action_detector.keras"
CANDIDATE = ML / "action_detector_candidate.keras"
REPORT = ML / "retrain_report.json"
EMB_CACHE = ML / "emb_cache" / "train_all-MiniLM-L6-v2.npy"
PROD_THRESHOLD = 0.90
NEW_DATA_WEIGHT = 8.0  # each labelled transcript line counts as 8 CSV rows (~30% of total weight)
SEED = 42


def set_seeds():
    random.seed(SEED)
    np.random.seed(SEED)
    tf.random.set_seed(SEED)


def build_model(dim):
    model = tf.keras.Sequential([
        tf.keras.layers.Input((dim,)),
        tf.keras.layers.Dense(256, activation="relu"),
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(128, activation="relu"),
        tf.keras.layers.Dense(1, activation="sigmoid"),
    ])
    model.compile("adam", "binary_crossentropy", metrics=["accuracy"])
    return model


def train(X, y, w):
    set_seeds()
    idx = np.random.permutation(len(X))
    cut = int(len(idx) * 0.85)
    fit, val = idx[:cut], idx[cut:]
    model = build_model(X.shape[1])
    model.fit(X[fit], y[fit], sample_weight=w[fit], validation_data=(X[val], y[val], w[val]),
              epochs=30, batch_size=256, verbose=0,
              callbacks=[tf.keras.callbacks.EarlyStopping(patience=4, restore_best_weights=True)])
    return model


def scores(y, prob, threshold):
    pred = prob >= threshold
    tp = int((pred & (y == 1)).sum()); fp = int((pred & (y == 0)).sum()); fn = int((~pred & (y == 1)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn,
            "flagged": tp + fp, "actual": tp + fn}


def best_threshold(y, prob):
    grid = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995, 0.999]
    return max(grid, key=lambda t: (scores(y, prob, t)["f1"], t))


def main():
    set_seeds()
    print("Loading data and embeddings...")
    embedder = SentenceTransformer("all-MiniLM-L6-v2")
    csv_df = pd.read_csv(CSV_TRAIN)
    y_csv = csv_df.label.astype(str).str.upper().eq("ACTION").astype(int).values
    if EMB_CACHE.exists() and len(np.load(EMB_CACHE)) == len(csv_df):
        X_csv = np.load(EMB_CACHE)
    else:
        X_csv = embedder.encode((csv_df.speaker.fillna("unknown") + ": " + csv_df.text.fillna("")).tolist(),
                                batch_size=256, normalize_embeddings=True, show_progress_bar=True).astype("float32")

    new = pd.read_csv(LABELLED)
    X_new = embedder.encode((new.speaker.fillna("unknown") + ": " + new.text.fillna("")).tolist(),
                            batch_size=128, normalize_embeddings=True).astype("float32")
    y_new = new.label.eq("ACTION").astype(int).values
    families = list(dict.fromkeys(new.family))
    prod = tf.keras.models.load_model(PROD_MODEL)
    prod_prob = prod.predict(X_new, verbose=0).ravel()

    folds, thresholds = {}, []
    for fam in families:
        test = (new.family == fam).values
        train_fams = new[~test]
        # validation slice of the TRAINING families, used only to pick the threshold
        rng = np.random.RandomState(SEED)
        val_mask = np.zeros(len(new), bool)
        for f in train_fams.family.unique():
            idx = np.where((new.family == f).values)[0]
            val_mask[rng.choice(idx, size=max(1, len(idx) // 4), replace=False)] = True
        tr = ~test & ~val_mask
        X = np.vstack([X_csv, X_new[tr]])
        y = np.concatenate([y_csv, y_new[tr]])
        w = np.concatenate([np.ones(len(y_csv)), np.full(tr.sum(), NEW_DATA_WEIGHT)])
        model = train(X, y, w)
        t = best_threshold(y_new[val_mask], model.predict(X_new[val_mask], verbose=0).ravel())
        thresholds.append(t)
        prob = model.predict(X_new[test], verbose=0).ravel()
        folds[fam] = {
            "lines": int(test.sum()), "commitments": int(y_new[test].sum()),
            "current_model": scores(y_new[test], prod_prob[test], PROD_THRESHOLD),
            "retrained": scores(y_new[test], prob, t), "retrained_threshold": t,
        }
        c, r = folds[fam]["current_model"], folds[fam]["retrained"]
        print(f"  held out {fam:20s} current F1 {c['f1']:.3f} (P {c['precision']:.2f} R {c['recall']:.2f}) "
              f"-> retrained F1 {r['f1']:.3f} (P {r['precision']:.2f} R {r['recall']:.2f}) @ {t}")

    macro = lambda key: round(float(np.mean([f[key]["f1"] for f in folds.values()])), 3)
    summary = {"current_macro_f1": macro("current_model"), "retrained_macro_f1": macro("retrained")}
    print(f"\nMacro F1 across held-out families: current {summary['current_macro_f1']} -> retrained {summary['retrained_macro_f1']}")

    print("Training final model on everything...")
    final_threshold = float(np.median(thresholds))
    X = np.vstack([X_csv, X_new]); y = np.concatenate([y_csv, y_new])
    w = np.concatenate([np.ones(len(y_csv)), np.full(len(y_new), NEW_DATA_WEIGHT)])
    final = train(X, y, w)
    improved = summary["retrained_macro_f1"] > summary["current_macro_f1"]
    final.save(CANDIDATE)
    if improved:
        final.save(PROD_MODEL)
    REPORT.write_text(json.dumps({
        "date": date.today().isoformat(), "families": folds, "summary": summary,
        "final_threshold": final_threshold, "fold_thresholds": thresholds,
        "deployed": improved, "new_data_weight": NEW_DATA_WEIGHT,
        "data": {"csv_rows": int(len(y_csv)), "labelled_lines": int(len(y_new)), "labelled_commitments": int(y_new.sum())},
    }, indent=2))
    print(f"Final threshold {final_threshold}. "
          + ("Deployed to ml/action_detector.keras." if improved else "NOT deployed (no improvement); saved as candidate."))


if __name__ == "__main__":
    main()
