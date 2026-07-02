# Analysis Instructions: Artificial Turf Mentions in Meeting Documents

## Objective
Determine whether a meeting document (agenda, minutes, or packet) contains any
discussion of artificial turf, synthetic turf, or related athletic surface
topics, and if so, summarize what was discussed and the apparent sentiment.

## Terms to search for (case-insensitive)
- artificial turf
- synthetic turf
- turf field / turf fields
- artificial grass
- synthetic grass
- field turf / fieldturf
- astroturf
- turf infill
- turf system
- turf replacement / turf install / turf installation
- athletic field (only flag if co-occurring with "turf" or "synthetic"/"artificial" surface context)
- sports turf

## What to extract if a match is found
1. **Meeting identifier**: date, meeting title, agenda item number/name.
2. **Quoted context**: the sentence(s) surrounding each match (~2-3 sentences).
3. **Topic type**: one of
   - Procurement / bid / contract award
   - Budget / capital expenditure
   - Facility construction or renovation project
   - Maintenance / replacement discussion
   - Policy or safety discussion (e.g., heat, injury, environmental)
   - General mention / informational only
4. **Sentiment**: one of
   - Positive (supportive comments, praise for performance/durability/cost savings)
   - Negative (concerns raised: cost, safety, environmental, heat, injury, opposition)
   - Neutral / factual (no evaluative language, just procedural mention)
   - Mixed (both positive and negative points raised)
5. **Decision/outcome**: was a motion made, approved, tabled, denied, or is it informational only?

## Output format
Return a structured result per document:

```
Document: <filename or meeting date/title>
Turf mentioned: Yes/No
If Yes:
  - Item: <agenda item # / section>
    Context: "<quoted excerpt>"
    Topic type: <type>
    Sentiment: <sentiment>
    Outcome: <outcome>
  (repeat per distinct mention/item)
Summary: <1-2 sentence overall summary of turf-related content in this document>
```

If no mention is found, simply return:
```
Document: <filename or meeting date/title>
Turf mentioned: No
```

## Notes
- Do not infer turf discussion from generic "field," "park," or "recreation"
  mentions unless directly tied to a turf/synthetic surface term.
- If the document is entirely unrelated (e.g., HR policy, finance audit with no
  facilities content), it is acceptable to return "No" quickly without deep
  extraction.
- Preserve original wording in quoted context; do not paraphrase the source
  when quoting.
