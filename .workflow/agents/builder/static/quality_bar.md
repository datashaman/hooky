# Builder Agent Quality Bar

A passing Builder Agent output:

- implements only behavior required by the approved tests and spec
- leaves approved tests untouched
- does not add hidden dependencies
- does not modify pipeline artifacts from earlier stages
- creates a runnable project in the working folder
- uses project-approved conventions and public TodoMVC DOM structure
- preserves localStorage compatibility with `id`, `title`, and `completed`
- supports hash routes `#/`, `#/active`, and `#/completed`
- produces a concise build report

Critical failures:

- editing approved tests
- omitting production implementation
- adding unrelated framework or build complexity
- creating tests instead of implementation
- inventing behavior outside the approved spec
- relying on non-browser mocks instead of browser behavior

