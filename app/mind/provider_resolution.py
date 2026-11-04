"""Source text establishes provenance; model guesses never establish authority."""
import re


def explicit_provider(provider, source, evidence=None):
    if not isinstance(provider, str) or not provider:
        return None
    source = str(source or '').casefold()
    if re.search(r"\b(no|sin|excepto|evita\w*|nunca|never|except|avoid)\b|don['’]t", source):
        return None  # Ambiguous/negative source text requires an explicit scoped choice.
    if evidence and str(evidence).casefold() not in source:
        return None
    token = r'[\s_-]+'.join(re.escape(p) for p in re.split(r'[_-]+', provider.casefold()))
    return provider if re.search(r'(?<!\w)' + token + r'(?!\w)', source) else None
