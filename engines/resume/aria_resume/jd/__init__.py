"""Stage 1, the JD Parser (RESUME_ENGINE §3): the rules, before any model reads the posting.

The posting is untrusted text (SECURITY.md §5). Everything here treats it as data: it is
cleaned of characters a reader cannot see, split into sections by its headings, searched
for the few things rules read reliably, and screened for instructions that have nothing
to do with applying. No line of it is ever followed.
"""
