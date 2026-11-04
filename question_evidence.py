"""Select short, page-labelled excerpts for a question about a whole article."""
from __future__ import annotations

import re
from math import log


# Section headings bridge common questions in Chinese and English-language PDFs.
SECTION_TERMS = (
    (r"公开|开放|下载|获取|获得|可用性|可及|存取|access|availab|public|download",
     ("data availability", "data and code availability", "accession", "download", "deposited", "available at", "available from", "portal", "数据可用性", "可下载")),
    (r"方法|设计|验证|怎么做|how did|method|validat",
     ("methods", "study design", "validation", "experimental procedures", "研究方法")),
    (r"主要发现|研究结果|结论|贡献|总结|综述|全文|整体|核心发现|主线|limitations|conclusion|overview|main finding|summari|summariz",
     ("abstract", "summary", "results", "discussion", "limitations", "结论", "讨论")),
)


def query_terms(text):
    words = re.findall(r"[a-z][a-z0-9_-]+", text.lower())
    for run in re.findall(r"[\u3400-\u9fff]{2,}", text):
        words.extend(run[i:i+2] for i in range(len(run) - 1))
    stop = {"the", "are", "of", "in", "this", "study", "paper", "and", "to", "how", "is", "does",
            "什么", "这个", "文章", "文中", "本文", "作者", "研究", "的是", "是否", "哪些", "如何"}
    return [term for term in dict.fromkeys(words) if term not in stop][:64]


def article_evidence(pages, content, question, previous_question=""):
    """Rank across every PDF page, using the guide to link Chinese terms to source pages."""
    texts = [text.lower() for text in pages]
    terms = query_terms(question)
    previous = query_terms(previous_question)[:24]
    weights = {}
    for term in dict.fromkeys(terms + previous):
        hits = sum(term in text for text in texts)
        weights[term] = (1 + log((1 + len(pages)) / (1 + hits))) * (1 if term in terms else 0.25)
    headings = [word for pattern, words in SECTION_TERMS if re.search(pattern, question, re.I) for word in words]
    def lexical(text):
        return sum(weight * min(text.count(term), 3) for term, weight in weights.items())

    sections = content.get("sections", [])
    guide_pages = {}
    section_scores = {}
    for entry in [*sections, *content.get("concepts", [])]:
        label = " ".join(str(entry.get(k, "")) for k in ("title", "guiding_question", "summary", "term", "aliases")).lower()
        relevance = lexical(label)
        section_scores[entry["id"]] = relevance
        for number in entry.get("pages", []):
            guide_pages[number] = max(guide_pages.get(number, 0), relevance)

    # For a long page keep the passage where the evidence actually occurs.
    ranked = []
    heading_matches = []
    linked_sources = []
    for number, (original, text) in enumerate(zip(pages, texts), 1):
        if not original.strip():
            continue
        best_score = -1
        for offset in range(0, len(text), 4400):
            part = text[offset:offset + 5600]
            words = lexical(part)
            section_score = 3 * sum(word in part for word in headings)
            score = words + section_score + min(words, section_score)
            if score > best_score:
                best_score, start = score, offset
            if offset + 5600 >= len(text):
                break
        direct = sum(weight for term, weight in weights.items() if term in text)
        ranked.append((best_score + min(guide_pages.get(number, 0), 15), direct, number, start))
        # Reserve space for the actual availability declaration as well as dataset-specific passages.
        excerpt = text[start:start + 5600]
        if "data availability" in headings:
            if any(h in excerpt for h in ("data availability", "data and code availability", "数据可用性")):
                heading_matches.append((direct, number))
            # A repository link near the queried dataset may be separate from the authors' own data statement.
            for link in re.finditer(r"https?://", excerpt):
                nearby = excerpt[max(0, link.start() - 180):link.end() + 100]
                if any(word in nearby for word in headings):
                    linked_sources.append((lexical(nearby), number))
    ranked.sort(key=lambda item: -item[0])
    candidates = {number: (score, start) for score, _, number, start in ranked}
    order = [number for score, _, number, _ in ranked if score > 0]
    # Retain the question's named entity as well as generic section-heading matches.
    if ranked:
        best_direct = max(ranked, key=lambda item: item[1])
        if best_direct[1] > 0:
            order.insert(0, best_direct[2])
    if heading_matches:
        order.insert(0, max(heading_matches, key=lambda item: item[0])[1])
    if linked_sources:
        score, number = max(linked_sources, key=lambda item: item[0])
        if score > 0:
            order.insert(0, number)
    # Give broad or low-match questions representative primary material from across the guide.
    anchors = [s["pages"][0] for s in sections if s.get("pages")] or sorted(candidates)
    if anchors:
        anchors = [anchors[i * (len(anchors) - 1) // 3] for i in range(4)]
    if "abstract" in headings:
        order = [*order[:2], 1, *anchors, *order[2:]]
    order = list(dict.fromkeys(order + [1, *anchors, *candidates]))
    selected = [n for n in order if n in candidates][:6]
    excerpts = [{"pdf_page": n, "text": pages[n-1][candidates[n][1]:candidates[n][1]+5600]} for n in selected]
    relevant_sections = [s for s in sections if set(s.get("pages", ())).intersection(selected)]
    relevant_sections.sort(key=lambda s: -section_scores[s["id"]])
    return excerpts, relevant_sections[:4]
