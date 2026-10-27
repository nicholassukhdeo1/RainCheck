from typing import Iterable
import unicodedata


def _is_ascii_alphanumeric(character: str) -> bool:
    return character.isascii() and character.isalnum()


def _contains_keyword(title_text: str, keyword: str) -> bool:
    keyword_text = unicodedata.normalize("NFKC", keyword).lower()
    if not keyword_text.strip():
        return False

    position = 0
    while True:
        start = title_text.find(keyword_text, position)
        if start == -1:
            return False

        end = start + len(keyword_text)
        # Only ASCII letters and digits at the keyword's outer edges need boundaries.
        left_matches = (
            not _is_ascii_alphanumeric(keyword_text[0])
            or start == 0
            or not _is_ascii_alphanumeric(title_text[start - 1])
        )
        right_matches = (
            not _is_ascii_alphanumeric(keyword_text[-1])
            or end == len(title_text)
            or not _is_ascii_alphanumeric(title_text[end])
        )
        if left_matches and right_matches:
            return True

        # A later occurrence may have valid boundaries even when this one does not.
        position = start + 1


def title_matches(title: str, keywords: Iterable[str], exclude_keywords: Iterable[str]) -> bool:
    """
    True if the title contains at least one include keyword and no exclude keyword.
    Both jobs call this, so Yahoo and eBay always agree on what counts as the same item.
    """
    title_text = unicodedata.normalize("NFKC", title).lower()

    # exclude wins: a title with a bad word is dropped even if it also has a good one
    if any(_contains_keyword(title_text, bad) for bad in exclude_keywords):
        return False

    # any() is True as soon as ONE include keyword is found; no keywords means no match
    return any(_contains_keyword(title_text, keyword) for keyword in keywords)
