# Add a slugify helper

We already have `textutil.collapse_ws` for squashing whitespace. Please add a `slugify` function we can import from `textutil` (re-exported from the package root is fine).

`slugify(text)` should:

- convert to lowercase
- replace any run of whitespace with a single hyphen
- remove characters that are not ASCII letters, digits, or hyphens
- strip leading and trailing hyphens
- collapse repeated hyphens into one

Empty / all-punctuation input should become an empty string. Do not modify the input string object in place (strings are immutable anyway; just don't rely on mutating a list the caller still holds if you build from one).
