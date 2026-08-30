"""Client-side tool retrieval - the fallback when a provider has no deferred loading.

Twenty domains at fifteen skills each is 300 tool schemas, which would cost more than
the conversation and measurably degrade selection. Where the provider cannot defer
loading, we select a top-K subset ourselves.

Scoring is lexical (a small BM25-ish overlap). That is deliberately modest: skill
descriptions are short and keyword-dense, so this recovers most of the benefit without
requiring embeddings or pgvector. It has a real failure mode, stated in docs/skills.md -
a skill described in words unlike those the user uses is invisible, and looks to the
user like a missing capability.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from gerent.skills.base import Skill

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or of to for in on with use used using this that it is are be "
    "returns return not do does when what which from into by as at".split()
)


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def rank(query: str, skills: list[Skill], top_k: int) -> list[Skill]:
    """Return the top_k skills most relevant to `query`, best first."""
    if len(skills) <= top_k:
        return list(skills)

    q = Counter(_tokens(query))
    if not q:
        return list(skills)[:top_k]

    docs = [
        Counter(_tokens(f"{s.name} {s.name.replace('_', ' ')} {s.description}")) for s in skills
    ]
    n = len(docs)
    df = Counter()
    for doc in docs:
        df.update(doc.keys())

    scored: list[tuple[float, int, Skill]] = []
    for index, (skill, doc) in enumerate(zip(skills, docs, strict=True)):
        length = sum(doc.values()) or 1
        score = 0.0
        for term, qn in q.items():
            if term not in doc:
                continue
            idf = math.log(1 + n / (1 + df[term]))
            score += qn * idf * (doc[term] / length)
        scored.append((score, -index, skill))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [s for score, _, s in scored if score > 0][:top_k] or list(skills)[:top_k]
