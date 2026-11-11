"""TF-IDF cosine similarity between WP files, so a drafter sees work that is already done or planned.

Duplicate WPs' titles, goals, deliverables and criteria share the topic's words while unrelated WPs share only the
project's vocabulary (crates, tests, captures), which inverse document frequency discounts.
Standard library only: tokens are lowercase words with light plural stemming, minus stopwords and
the WP format's own vocabulary; weights are sublinear term frequency times smoothed IDF, and vectors
are L2-normalised so a dot product is the cosine. The title counts twice: it is the WP's summary.

An initial calibration corpus contained 188 WPs (17,578 pairs); known duplicates scored 0.65
and 0.31 while 99% of pairs scored under 0.23. Tune the warning threshold for each project.
Adjacent-word bigrams were tried and dropped: they lowered every score without ranking the known
duplicates higher.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import wpfile

# The sections that say what a WP delivers; Context and Steps carry history and method, which
# related but distinct WPs share (adding Context ranked the known duplicates lower).
COMPARED_SECTIONS = ("Goal", "Deliverables", "Acceptance criteria")
TITLE_WEIGHT = 2
TOKEN = re.compile(r"[a-z][a-z0-9_]*[a-z0-9]")
# WP ids and the per-WP install-test prefixes (`wp0904_`) name a WP, not a topic.
ID_TOKEN = re.compile(r"^wp\d+[a-z0-9_]*$")
MINIMUM_TOKEN_LENGTH = 3
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing done down during each either else even every few for from further had has have
having here how if in into is it its itself just less like may might more most much must no nor not now of off on
once one only or other our out over own per same shall should since so some such than that the their them then
there these they this those through thus to too two under until up upon very via was we were what when where which
while who whom why will with within without would yet you your
""".split())  # noqa: SIM905 (a word list reads better as text)
# The WP format's and the project's working vocabulary: present in most WPs, so it ranks no pair.
FORMAT_WORDS = frozenset("""
wp wps test tests tested testing cargo crate crates src lib main docs doc md rs png log logs ron json toml
feature features install installs verify criterion criteria acceptance deliverable deliverables goal goals
step steps prefix named name names run runs ran check checks checked gate gates evidence capture captures
screenshot screenshots plan design file files new existing exist exists show shows shown nocapture release
example examples bin output outputs pass passes passed fail fails failed user users handoff baseline measure
measured table record recorded update updated
""".split())  # noqa: SIM905 (a word list reads better as text)


def stem(token: str) -> str:
    """Folds plurals so 'garments' and 'garment' are one term; identifiers keep their underscores."""
    if "_" in token or len(token) <= 4:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("sses"):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokens(text: str) -> list[str]:
    """The content terms of a text, in order."""
    terms = []
    for raw in TOKEN.findall(text.lower()):
        if len(raw) < MINIMUM_TOKEN_LENGTH or raw in STOPWORDS or ID_TOKEN.match(raw):
            continue
        term = stem(raw)
        if term in FORMAT_WORDS or raw in FORMAT_WORDS:
            continue
        terms.append(term)
    return terms


def compared_text(wp: wpfile.WorkPackage) -> str:
    """What a WP delivers: its title (counted TITLE_WEIGHT times), Goal, Deliverables and Acceptance criteria."""
    text = wp.text()
    return "\n".join([*[wp.title] * TITLE_WEIGHT, *(line for heading in COMPARED_SECTIONS for line in wpfile.section_lines(text, heading))])


@dataclass(frozen=True)
class SimilarityIndex:
    """L2-normalised TF-IDF vectors of a set of documents, keyed by WP id."""

    vectors: Mapping[str, Mapping[str, float]]

    @classmethod
    def build(cls, documents: Mapping[str, str]) -> SimilarityIndex:
        counted = {key: Counter(tokens(text)) for key, text in documents.items()}
        document_frequency: Counter = Counter()
        for counts in counted.values():
            document_frequency.update(counts.keys())
        total = len(counted)
        idf = {term: math.log((1 + total) / (1 + frequency)) + 1 for term, frequency in document_frequency.items()}
        vectors = {}
        for key, counts in counted.items():
            weights = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items()}
            norm = math.sqrt(sum(weight * weight for weight in weights.values()))
            vectors[key] = {term: weight / norm for term, weight in weights.items()} if norm else {}
        return cls(vectors)

    @classmethod
    def of_wps(cls, wps: Iterable[wpfile.WorkPackage]) -> SimilarityIndex:
        return cls.build({wp.id: compared_text(wp) for wp in wps})

    def cosine(self, first: str, second: str) -> float:
        smaller, larger = sorted((self.vectors[first], self.vectors[second]), key=len)
        return sum(weight * larger.get(term, 0.0) for term, weight in smaller.items())

    def most_similar(self, key: str, threshold: float) -> list[tuple[str, float]]:
        """The other documents at or above `threshold`, most similar first."""
        scores = [(other, self.cosine(key, other)) for other in self.vectors if other != key]
        return sorted(((other, score) for other, score in scores if score >= threshold), key=lambda pair: (-pair[1], pair[0]))

    def pairs_above(self, threshold: float, involving: Iterable[str] | None = None) -> list[tuple[str, str, float]]:
        """Every unordered pair at or above `threshold` (with one side in `involving`, when given),
        most similar first."""
        keys = sorted(self.vectors)
        wanted = set(keys if involving is None else involving)
        pairs = []
        for index, first in enumerate(keys):
            for second in keys[index + 1:]:
                if first not in wanted and second not in wanted:
                    continue
                score = self.cosine(first, second)
                if score >= threshold:
                    pairs.append((first, second, score))
        return sorted(pairs, key=lambda pair: (-pair[2], pair[0], pair[1]))
