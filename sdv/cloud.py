"""Cloud case store: mirror cases and reviewer decisions to Cloud Firestore.

Firestore's free Spark plan needs no credit card. Setup (one time, in the Firebase console):
  1. Create a project, then Build > Firestore Database > Create (production mode).
  2. Project settings > Service accounts > Generate new private key. Save the JSON OUTSIDE the repo.
  3. Put its path in .env as  GOOGLE_APPLICATION_CREDENTIALS=<path to that JSON>
  4. pip install google-cloud-firestore
The key file is a secret: never commit it (the .gitignore excludes *serviceaccount*.json and firebase-*.json).
"""
from __future__ import annotations

from typing import Optional

COLLECTION = "cases"


def _client(project: Optional[str] = None):
    try:
        from google.cloud import firestore  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Firestore sync needs:  pip install google-cloud-firestore") from e
    return firestore.Client(project=project) if project else firestore.Client()


def push_cases(store, client=None, project: Optional[str] = None, collection: str = COLLECTION) -> int:
    """Upsert every case (with any reviewer decision) as one Firestore document keyed by email_id."""
    client = client or _client(project)
    n = 0
    for c in store.list():
        client.collection(collection).document(c["email_id"]).set(c)
        n += 1
    return n


def pull_decisions(store, client=None, project: Optional[str] = None, collection: str = COLLECTION) -> int:
    """Copy decisions made elsewhere (e.g. a teammate's console) back into the local store."""
    client = client or _client(project)
    n = 0
    for doc in client.collection(collection).stream():
        d = doc.to_dict() or {}
        rv = d.get("review")
        if rv and rv.get("decision") and store.get(doc.id) is not None and not (store.get(doc.id) or {}).get("review"):
            store.review(doc.id, rv["decision"], rv.get("note", ""), rv.get("reviewer", ""))
            n += 1
    return n
