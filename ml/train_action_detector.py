"""
Trains the commitment/action-detector classifier used by transcript_parser.py.

Adapted from the original Colab prototype (Meeting_Transcript_Definition.py,
section 2) — same model architecture and training procedure, just pointed at
local paths instead of Colab's /content filesystem. This is a one-time
(or run-when-the-training-data-changes) offline step, not something the
Flask app runs itself.

Input:
    train_utterances_balanced.csv / test_utterances_balanced.csv in the
    project root (speaker, text, label columns; label is 'ACTION' or
    'DIALOG' for the rows this script cares about).

Output:
    ml/action_detector.keras — loaded lazily by transcript_parser.py at
    inference time.

Run:
    pip install -r requirements.txt
    python -m spacy download en_core_web_sm
    python ml/train_action_detector.py
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import tensorflow as tf
from sentence_transformers import SentenceTransformer
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split

ML_DIR = Path(__file__).parent
PROJECT_ROOT = ML_DIR.parent
TRAIN_CSV = PROJECT_ROOT / "train_utterances_balanced.csv"
TEST_CSV = PROJECT_ROOT / "test_utterances_balanced.csv"
EMB_CACHE_DIR = ML_DIR / "emb_cache"
MODEL_PATH = ML_DIR / "action_detector.keras"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"


def load(path):
    df = pd.read_csv(path)
    x = (df.speaker.fillna("unknown") + ": " + df.text.fillna("")).tolist()
    y = df.label.astype(str).str.upper().eq("ACTION").astype(int).values
    return x, y


def embed(embedder, texts, tag):
    EMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = EMB_CACHE_DIR / f"{tag}_{EMBEDDING_MODEL_NAME}.npy"
    if cache.exists():
        cached = np.load(cache)
        if len(cached) == len(texts):
            return cached
    e = embedder.encode(
        texts, batch_size=256, convert_to_numpy=True,
        normalize_embeddings=True, show_progress_bar=True,
    ).astype("float32")
    np.save(cache, e)
    return e


def main():
    embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
    emb_dim = embedder.get_sentence_embedding_dimension()
    print("Ready. Embedding dim =", emb_dim)

    x_train, y_train = load(TRAIN_CSV)
    x_test, y_test = load(TEST_CSV)
    X_train, X_test = embed(embedder, x_train, "train"), embed(embedder, x_test, "test")
    print(f"{len(X_train):,} train / {len(X_test):,} test")

    # validation comes out of TRAIN so the test set stays untouched until the end
    X_fit, X_val, y_fit, y_val = train_test_split(
        X_train, y_train, test_size=0.15, stratify=y_train, random_state=42
    )

    model = tf.keras.Sequential([
        tf.keras.layers.Input((emb_dim,)),
        tf.keras.layers.Dense(256, activation="relu"),
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(128, activation="relu"),
        tf.keras.layers.Dense(1, activation="sigmoid"),
    ])
    model.compile("adam", "binary_crossentropy", metrics=["accuracy"])
    model.fit(
        X_fit, y_fit, validation_data=(X_val, y_val),
        epochs=30, batch_size=256, verbose=2,
        callbacks=[tf.keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True)],
    )

    prob = model.predict(X_test, verbose=0).ravel()
    print(classification_report(y_test, prob >= 0.5, target_names=["DIALOG", "ACTION"], digits=4))
    print("ROC-AUC:", round(roc_auc_score(y_test, prob), 4))

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    model.save(MODEL_PATH)
    print(f"Saved model to {MODEL_PATH}")


if __name__ == "__main__":
    main()
