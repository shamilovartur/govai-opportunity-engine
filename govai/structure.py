"""Conservative NPA segmentation. All offsets refer to the unchanged extracted text.

This is a rule-based aid to review, not a legal parser. Missing data stays null;
only observed markers create numbered clauses. Repeated text is never deduplicated.
"""

import re
from datetime import datetime
from hashlib import sha256

NUMBER = re.compile(r"^\s*(?P<marker>(?P<number>\d+(?:\.\d+)*)(?P<end>[.)]))(?:[ \t]+|$)")
LETTER = re.compile(r"^\s*(?P<marker>(?P<number>[а-яёa-z])\))(?:[ \t]+|$)")
SECTION = re.compile(r"^\s*(?:[IVXLCDM]+\.\s+|(?:Раздел|Глава)\s+\S+)", re.I)
APPENDIX = re.compile(r"^\s*Приложение\s+(?:N|№)\s*\d+", re.I)
DATE = r"(?:\d{2}\.\d{2}\.\d{4}|\d{1,2}\s+[а-я]+\s+\d{4})"
ACT = re.compile(
    rf"(?im)^[ \t]*(?P<type>приказ|постановление|распоряжение|указ|федеральный закон)"
    rf"\b[^\n]{{0,100}}?[ \t]*(?:\n[ \t]*)?от\s+(?P<date>{DATE})"
    r"[ \t]*(?:г\.?)?[ \t]*(?:№|N)[ \t]*(?P<number>[\w/-]+)"
)
MONTHS = dict(zip(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(),
    range(1, 13),
))


def _date(raw):
    try:
        if "." in raw:
            return datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
        day, month, year = raw.lower().split()
        return datetime(int(year), MONTHS[month], int(day)).date().isoformat()
    except (ValueError, KeyError):
        return None


def _evidence(text, start, end, value=None):
    return {"value": text[start:end] if value is None else value,
            "start": start, "end": end, "text": text[start:end]}


def _metadata(text, first_clause):
    # Restrict identity/edition searches to the document preamble, never citations
    # inside clauses or a filename. Conflicting preamble identities stay unknown.
    prefix = text[:min(first_clause, 12000)]
    result = dict.fromkeys(("act_type", "number", "adoption_date", "title", "revision_date"))
    matches = list(ACT.finditer(prefix))
    identities = {(m["number"], _date(m["date"])) for m in matches}
    if len(identities) == 1:
        match = matches[0]
        for key, group in (("act_type", "type"), ("number", "number"), ("adoption_date", "date")):
            value = _date(match[group]) if key == "adoption_date" else match[group]
            if value is not None:
                result[key] = _evidence(text, *match.span(group), value)
        # A quoted title following the act heading, or an explicit uppercase title.
        tail = prefix[match.end():]
        quoted = re.search(r'["«]((?:Об? |ОБ? )[^"»]+)["»]', tail)
        upper = re.search(r"(?m)^ОБ? [А-ЯЁ][^\n]*(?:\n[А-ЯЁ(][А-ЯЁ(),. \-]+)*", tail)
        title = quoted or upper
        if title:
            a, b = title.span(1 if quoted else 0)
            result["title"] = _evidence(text, match.end() + a, match.end() + b)
    # A modifying act's date is NOT automatically the whole document's edition.
    edition_pattern = rf"(?:\(\s*(?:в\s+)?ред(?:\.|акции)|редакция)\s+от\s*({DATE})"
    editions = [(m.start(1), m.end(1), _date(m[1]))
                for m in re.finditer(rf"(?im)^[ \t]*{edition_pattern}", prefix)]
    if len(identities) == 1:
        for match in matches:
            line_end = prefix.find('\n', match.end())
            line_end = len(prefix) if line_end == -1 else line_end
            for m in re.finditer(edition_pattern, prefix[match.end():line_end], re.I):
                editions.append((match.end() + m.start(1), match.end() + m.end(1), _date(m[1])))
    if editions and len({e[2] for e in editions}) == 1 and editions[0][2]:
        start, end, value = editions[0]
        result["revision_date"] = _evidence(text, start, end, value)
    return result


def _heading_candidate(line):
    """Only a hint: punctuation-free short headings also occur in ordinary prose."""
    stripped = line.strip()
    return (bool(stripped) and len(stripped) <= 180 and stripped[0].isupper()
            and not re.search(r"[.;:!?]$", stripped) and "\t" not in stripped
            and not NUMBER.match(stripped) and not stripped.startswith(("-", "(")))


def structure_document(filename, digest, extension, parts, headings, input_warnings):
    text = "\n\n".join(parts)
    pages, fragments, sections, clauses = [], [], [], []
    warnings = list(input_warnings)
    if extension != "pdf":
        warnings.append("source_pagination_unavailable")
    warnings.append("structure_is_heuristic_review_required")
    offset = 0
    section_id = heading_id = active_id = base_id = None
    active_numbers = {}
    list_id = None
    list_uncertain = False
    blank_before = True
    first_clause = len(text)

    # Repeated edge text is retained as a layout candidate, not silently removed.
    edge_counts = {}
    if extension == "pdf":
        for part in parts:
            nonempty = [s.strip() for s in part.splitlines() if s.strip()]
            for edge in set(nonempty[:2] + nonempty[-2:]):
                edge_counts[edge] = edge_counts.get(edge, 0) + 1

    def close_clause():
        nonlocal active_id, base_id, list_id, list_uncertain
        active_id = base_id = list_id = None
        list_uncertain = False

    for page_index, part in enumerate(parts):
        page_number = page_index + 1 if extension == "pdf" else None
        if page_index:
            fragments.append({"id": f"f-{offset}", "text": "\n\n", "start": offset,
                              "end": offset + 2, "page_number": None, "line_number": None,
                              "kind": "page_separator", "section_id": section_id,
                              "heading_id": heading_id, "clause_id": None,
                              "attribution": "not_applicable"})
            offset += 2
        pages.append({"number": page_number, "start": offset, "end": offset + len(part)})
        lines = part.splitlines(keepends=True)
        nonempty_indices = [i for i, s in enumerate(lines) if s.strip()]
        edges = set(nonempty_indices[:2] + nonempty_indices[-2:])
        for line_number, line in enumerate(lines, 1):
            stripped = line.strip()
            marker = NUMBER.match(stripped) or LETTER.match(stripped)
            # Dates at line starts are not clause numbers.
            if re.match(r"^\d{2}\.\d{2}\.\d{4}\b", stripped):
                marker = None
            known_heading = any(start <= offset < end for start, end in headings)
            explicit_heading = bool(SECTION.match(stripped) or APPENDIX.match(stripped))
            candidate = (not marker and not known_heading and not explicit_heading
                         and blank_before and _heading_candidate(line))
            kind, attribution = "text", "continuation" if active_id else "unassigned"
            owner = active_id
            layout = (extension == "pdf" and line_number - 1 in edges
                      and (stripped.isdigit() or edge_counts.get(stripped, 0) > 1))
            if not stripped:
                kind = "whitespace"
            elif layout:
                kind, owner, attribution = "layout_candidate", None, "uncertain"
            elif explicit_heading or known_heading or candidate:
                kind = "section" if explicit_heading or known_heading else "heading_candidate"
                attribution = "explicit_heading" if kind == "section" else "uncertain"
                close_clause()
                owner = None
                heading_id = f"s-{offset}"
                parent_section = section_id
                if explicit_heading or known_heading:
                    section_id = heading_id
                    active_numbers.clear()
                    parent_section = None
                sections.append({"id": heading_id, "text": line, "start": offset,
                                 "end": offset + len(line), "kind": kind,
                                 "parent_id": parent_section})
            elif stripped.startswith(("КонсультантПлюс:", "(в ред.", "(п. ", "(см. ")):
                kind, owner, attribution = "annotation", None, "unassigned"
            elif re.match(r"^(?:<\d+>|\[\d+\])", stripped):
                kind, owner, attribution = "footnote", None, "unassigned"
                close_clause()
            elif marker:
                list_uncertain = False
                first_clause = min(first_clause, offset)
                number, observed = marker["number"], marker["marker"]
                parent = None
                is_list = observed.endswith(")")
                if is_list:
                    parent = base_id
                else:
                    prefix = number.rsplit(".", 1)[0] if "." in number else None
                    parent = active_numbers.get(prefix)
                    active_numbers = {n: cid for n, cid in active_numbers.items()
                                      if number.startswith(n + ".")}
                active_id = f"c-{offset}"
                clauses.append({"id": active_id, "number": number, "marker": observed,
                                "parent_id": parent, "section_id": section_id,
                                "heading_id": heading_id,
                                "start": offset, "marker_start": offset + line.index(observed),
                                "marker_end": offset + line.index(observed) + len(observed)})
                if is_list:
                    list_id = active_id
                else:
                    base_id, list_id = active_id, None
                    active_numbers[number] = active_id
                kind, owner, attribution = "clause_start", active_id, "explicit_marker"
            elif not active_id and heading_id and len(stripped) <= 180 and not re.search(r"[.;:!?]$", stripped):
                kind, owner, attribution = "heading_continuation", None, "uncertain"
                sections[-1]["end"] = offset + len(line)
                sections[-1]["text"] = text[sections[-1]["start"]:sections[-1]["end"]]
            elif list_id and (blank_before or list_uncertain):
                # A paragraph following a list may belong to the last item or its
                # parent. Preserve it without asserting either legal attribution.
                owner, attribution = None, "uncertain"
                list_uncertain = True
                warnings.append("ambiguous_list_continuation")
            fragment = {"id": f"f-{offset}", "text": line, "start": offset,
                        "end": offset + len(line), "page_number": page_number,
                        "line_number": line_number, "kind": kind,
                        "section_id": section_id, "heading_id": heading_id, "clause_id": owner,
                        "attribution": attribution}
            fragments.append(fragment)
            blank_before = not bool(stripped)
            offset += len(line)

    # End offsets delimit direct attributed fragments, never swallowed headings.
    owners = {}
    for fragment in fragments:
        owners.setdefault(fragment['clause_id'], []).append(fragment)
    for clause in clauses:
        owned = owners[clause['id']]
        clause["fragment_ids"] = [f["id"] for f in owned]
        clause["end"] = owned[-1]["end"]
        clause["page_numbers"] = sorted({f["page_number"] for f in owned if f["page_number"]})
    if not clauses:
        warnings.append("numbered_clauses_not_recognized")
    return {"schema_version": "1.0", "source": {"filename": filename, "sha256": digest,
            "text_sha256": sha256(text.encode('utf-8')).hexdigest(),
            "format": extension, "page_number_basis": "pdf_physical_index_1_based" if extension == "pdf" else None},
            "text": text, "metadata": _metadata(text, first_clause), "pages": pages,
            "sections": sections, "clauses": clauses, "fragments": fragments,
            "warnings": list(dict.fromkeys(warnings))}
