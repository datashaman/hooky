# TodoMVC Fixture Project

This is a React project-context fixture for evaluating Builder Agent implementation.

It is intentionally separate from the Hooky repository. During eval, this folder is copied into each model-specific working folder under `.workflow/eval-runs/test-agent/<timestamp>/<model>/`.

The fixture supplies project context only. It does not contain an implementation because the Test Agent is responsible for producing tests from an approved specification, not building the app.
