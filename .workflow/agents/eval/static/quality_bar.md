# Eval Agent Quality Bar

A passing Eval Agent output:

- refuses to evaluate failed verification as merge-safe
- distinguishes deterministic correctness from engineering quality
- catches overengineering and architectural mismatch even when tests pass
- identifies hidden assumptions and review focus
- uses the actual artifacts and implementation rather than generic advice
- provides defensible scores with concise evidence

Critical failures:

- passing failed verification
- ignoring agent traces or prior-stage reports
- unexplained low scores
- missing hidden assumptions
- treating all passing tests as merge-safe
- editing files

