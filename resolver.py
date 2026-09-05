"""
Entity & Vendor Resolver
========================
Solves Trap #2 (Vendor name variations) and Trap #4 (Two distinct failure modes).

Outcomes:
1. MATCH: Confident canonical vendor identified.
2. AMBIGUOUS: Query matches multiple vendors closely (e.g., 'Amazon' -> 'Amazon Web Services' vs 'Amazon Logistics').
3. NOT_FOUND: No matching vendor in dataset (e.g., 'Netflix').
"""

import re
from rapidfuzz import fuzz
import config

LEGAL_SUFFIXES = {"inc", "corp", "corporation", "ltd", "llc", "co", "company", "gmbh", "sa", "plc"}

def get_candidate_acronyms(vendor_name: str) -> list[str]:
    """Generates candidate acronyms for any vendor name (e.g. 'Amazon Web Services, Inc.' -> ['aws', 'awsi'])."""
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', vendor_name).lower() #if the letters or not alphabets and numbers replace with space
    all_words = cleaned.split()
    filtered_words = [w for w in all_words if w not in LEGAL_SUFFIXES]
    
    candidates = [] #suppose we have filtered words=[amazon,web,services]
    if len(filtered_words) >= 2:
        candidates.append("".join(w[0] for w in filtered_words)) #the first letter of evry word like it will becoem aws 
    if len(all_words) >= 2:
        full_acr = "".join(w[0] for w in all_words)
        if full_acr not in candidates:
            candidates.append(full_acr)
    return candidates

def resolve_vendor(user_input_vendor: str, known_vendors: list):
    """
    Fuzzy resolves a user-entered vendor string against the list of known vendors.
    
    Returns:
        status (str): "MATCH", "AMBIGUOUS", "NOT_FOUND", or "NONE"
        resolved_entity (str or list or None): canonical vendor name or list of candidate names
        confidence (float): match score (0.0 to 1.0)
    """
    if not user_input_vendor or not user_input_vendor.strip():
        return "NONE", None, 1.0
        
    query = user_input_vendor.strip().lower()
    
    # 1. Exact case-insensitive match check
    for v in known_vendors:
        if query == v.lower():
            return "MATCH", v, 1.0

    # 2. Config alias map (domain-specific manual overrides)
    custom_aliases = getattr(config, "VENDOR_ALIASES", {})
    if query in custom_aliases and custom_aliases[query] in known_vendors:
        return "MATCH", custom_aliases[query], 0.98

    # 3. Dynamic Acronym Matching (e.g. AWS -> Amazon Web Services, GCP -> Google Cloud Platform)
    acronym_matches = []
    for v in known_vendors:
        acrs = get_candidate_acronyms(v)
        if query in acrs:
            acronym_matches.append(v)
    if len(acronym_matches) == 1: #if only one acronym matches like suppose only one aws it is matched
        return "MATCH", acronym_matches[0], 0.98
    elif len(acronym_matches) > 1: #suppose used asked amazon so it will match amazon web services and amazon logistics so it will be ambiguous
        return "AMBIGUOUS", acronym_matches, 0.90

    # 4. Direct Substring / Word-containment matching (handles "Amazon", "CloudScale", etc.)
    # Clean punctuations for word comparison
    contained = []
    for v in known_vendors:
        clean_v = v.lower().replace(",", "").replace(".", "").replace("-", " ")
        words = clean_v.split()
        if query in clean_v or any(query == w for w in words):
            contained.append(v)
            
    if len(contained) == 1:
        return "MATCH", contained[0], 0.95
    elif len(contained) > 1:
        # Failure Mode 2: Multiple vendors contain the query (e.g. "Amazon" -> AWS & Amazon Logistics)
        return "AMBIGUOUS", contained[:3], 0.90

    # 5. Fuzzy Scoring via WRatio
    scores = []
    for v in known_vendors:
        score = fuzz.WRatio(query, v) #calcuate similarity score between query and vendor name
        scores.append((v, score))
        
    scores.sort(key=lambda x: x[1], reverse=True)
    top_name, top_score = scores[0]
    
    if top_score >= 70:
        # Check if 2nd candidate is very close (Ambiguous)
        if len(scores) > 1:
            second_name, second_score = scores[1]
            if second_score >= 68 and (top_score - second_score < 10):
                return "AMBIGUOUS", [top_name, second_name], round(top_score / 100.0, 2)
        return "MATCH", top_name, round(top_score / 100.0, 2)
        
    # Failure Mode 1: Vendor does not exist
    return "NOT_FOUND", None, 0.0

