"""A local sparse vector store. Rule text and thresholds come from documents."""
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path


def vector(text):
    counts = Counter(re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")))
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1
    return {k: v / norm for k, v in counts.items()}


class KnowledgeBase:
    def __init__(self, directory: Path, state_dir: Path):
        self.path = directory / "underwriting_rules.md"
        source = self.path.read_text()
        self.digest = hashlib.sha256(source.encode()).hexdigest()
        self.documents = []
        for chunk in source.split("\n## ")[1:]:
            title, body = chunk.split("\n", 1)
            match = re.search(r"<!-- rule: (.*?) -->", body)
            if not match:
                raise ValueError(f"Rule metadata missing: {title}")
            metadata = json.loads(match.group(1))
            text = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
            rule_id = title.split()[0]
            self.documents.append({"rule_id": rule_id, "title": title, "text": text,
                                   "metadata": metadata, "source": f"knowledge/underwriting_rules.md#{rule_id}",
                                   "vector": vector(title + " " + text)})
        if not self.documents:
            raise ValueError("Knowledge base contains no rules.")
        if len({d['rule_id'] for d in self.documents}) != len(self.documents):
            raise ValueError("Knowledge rule IDs must be unique.")
        state_dir.mkdir(parents=True, exist_ok=True)
        # Content-addressed vectors avoid stale retrieval when rule text changes.
        self.index_path = state_dir / f"knowledge-{self.digest[:16]}.json"
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"sha256": self.digest, "documents": self.documents}, indent=2))
        self.documents = json.loads(self.index_path.read_text())["documents"]

    def retrieve(self, claim_type, query):
        q = vector(query)
        docs = []
        for document in self.documents:
            scopes = document["metadata"]["claim_types"]
            if "*" not in scopes and claim_type not in scopes:
                continue
            score = sum(q.get(k, 0) * v for k, v in document["vector"].items())
            docs.append({k: v for k, v in document.items() if k != "vector"} | {"score": round(score, 4)})
        # Retrieve ALL applicable controls. Top-k truncation could hide a mandatory rule.
        return sorted(docs, key=lambda d: (-d["score"], d["rule_id"]))
