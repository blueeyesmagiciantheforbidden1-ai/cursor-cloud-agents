def truncate(text, max_len, *, word_boundary=False):
    if max_len < 3:
        raise ValueError('max_len must be >= 3')
    if len(text) <= max_len:
        return text
    budget = max_len - 3
    cut = text[:budget]
    if word_boundary and budget < len(text) and text[budget] != ' ':
        sp = cut.rfind(' ')
        if sp != -1:
            cut = cut[:sp]
    return cut + '...'
