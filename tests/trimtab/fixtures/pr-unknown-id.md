Switches the Zoho client tests to an in-memory fake.

## Harness items applied
```yaml
- id: TST-99
  why: our own HTTP client is not a true external system, so a fake rather than a mock
  files: [tests/unit/test_zoho_client.py]
```

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
