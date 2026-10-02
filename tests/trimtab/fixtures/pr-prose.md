Switches the Zoho client tests to an in-memory fake.

## Harness items applied
Applied TST-2: our own HTTP client gets a fake, not a mock.

## Harness feedback
```yaml
- id: TST-2
  kind: ambiguous
  scope: upstream
  evidence: "in-memory fake vs mock is undecided for our own HTTP client"
```

## Process cost
```yaml
plan:   { tokens: 61000, seconds: 240 }
review:
  - { pass: reviewer, status: ran, tokens: 213000, seconds: 947, findings: 3 }
```
