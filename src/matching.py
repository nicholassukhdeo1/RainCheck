from typing import Iterable


def title_matches(title: str, keywords: Iterable[str], exclude_keywords: Iterable[str]) -> bool:
    """
    True if the title contains at least one include keyword and no exclude keyword.
    Both jobs call this, so Yahoo and eBay always agree on what counts as the same item.
    """
    # only the title is lowercased: keywords are already lowercased when they are saved
    # (validate_catalog), so a keyword with capitals never matches and the bad data shows up
    title_text = title.lower()

    # exclude wins: a title with a bad word is dropped even if it also has a good one
    if any(bad in title_text for bad in exclude_keywords):
        return False

    # any() is True as soon as ONE include keyword is found; no keywords means no match
    return any(keyword in title_text for keyword in keywords)
