## 1. OpenSpec

- [x] 1.1 Add proposal, design, and delta spec for result skill metrics.

## 2. Backend Models

- [x] 2.1 Add result-save fields for batch user count, batch skill coverage,
      topic skill coverage, and top skill.
- [x] 2.2 Add item-level metric validation.
- [x] 2.3 Add batch-level consistency validation.
- [x] 2.4 Allow array-shaped `bbk_dis` in the save payload.

## 3. Backend Persistence

- [x] 3.1 Add the new columns and values to the result INSERT.
- [x] 3.2 Preserve the existing delete-then-batch-insert transaction strategy.

## 4. Verification

- [x] 4.1 Add focused tests for successful persistence and validation failures.
- [ ] 4.2 Run the focused high-frequency question tests.
- [x] 4.3 Run OpenSpec validation for this change.
