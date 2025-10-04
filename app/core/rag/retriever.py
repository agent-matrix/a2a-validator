# app/core/rag/retriever.py
from __future__ import annotations
import json, logging, os
from pathlib import Path
from typing import List, Dict, Optional
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

class Retriever:
    def __init__(self, kb_path: str = "data/kb.jsonl",
                 model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
                 top_k: int = 4):
        self.kb_path = Path(kb_path)
        self.top_k = top_k
        if not self.kb_path.exists():
            raise FileNotFoundError(f"KB file not found: {self.kb_path} (jsonl with {{text,source}})")

        # Use a project-local cache to avoid '/.cache' permission issues
        cache_dir = Path(os.getenv("HF_HOME", "./.cache"))
        cache_dir.mkdir(parents=True, exist_ok=True)

        self.model = SentenceTransformer(model_name, cache_folder=str(cache_dir))

        self.docs: List[Dict[str, str]] = []
        with self.kb_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                self.docs.append(json.loads(line))
        texts = [d["text"] for d in self.docs]
        emb = self.model.encode(texts, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)
        self.dim = int(emb.shape[1])
        self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(emb.astype("float32"))

    def retrieve(self, query: str, k: Optional[int] = None) -> List[Dict]:
        k = k or self.top_k
        vec = self.model.encode([query], convert_to_numpy=True, normalize_embeddings=True)
        D, I = self.index.search(vec.astype("float32"), k)
        out: List[Dict] = []
        for idx, score in zip(I[0], D[0]):
            if int(idx) < 0:
                continue
            d = self.docs[int(idx)]
            out.append({"text": d["text"], "source": d.get("source", f"kb:{idx}"), "score": float(score)})
        return out
