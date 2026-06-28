# Test Agent Quality Bar

- Generate behavior-focused tests, not implementation-coupled tests.
- Cover every approved acceptance criterion from the Spec Agent contract.
- Include realistic user workflows and edge cases from the approved spec.
- Do not write production code.
- Do not add dependencies unless explicitly approved by the spec.
- Prefer browser-level or DOM-level tests for UI behavior.
- Use accessible selectors when the eventual implementation can reasonably provide them.
- Include localStorage setup and cleanup when testing persistence.
- Include route setup and cleanup when testing route-filter behavior.
- Make test filenames and fixture names concrete and predictable.
- If generated tests are run before implementation, failures must be meaningful red-phase behavior failures.
- Do not treat setup, syntax, configuration, server, selector, permission, or runtime harness errors as a valid red phase.
