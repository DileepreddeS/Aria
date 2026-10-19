# Real postings for the resume eval

The `resume` suite (`uv run python -m evals.run resume`) runs every posting here. They are
reported, never gated, until step 12 of `docs/PHASE2_PLAN.md`: their labels are judgement,
not consistency.

Layout, one directory per role type:

```
jds/<role>/<id>.txt      the posting text, as captured
jds/<role>/<id>.yaml     source_url, capture_date (YYYY-MM-DD), and the labels
```

Before a posting is committed, recruiter names, emails and phone numbers are removed by hand.
Company names stay (RESUME_ENGINE §11). Nothing about a real candidate goes here; the eval's
candidate is the synthetic one in `../candidate.yaml`.

As with the synthetic set, a label changes only after re-reading the posting text, with the
reason recorded, never to make a run agree.
