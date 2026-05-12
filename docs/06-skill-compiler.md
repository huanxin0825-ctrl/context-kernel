# Skill Compiler

The first compiler is deterministic and local. It converts structured Markdown into the Context Kernel skill JSON schema.

## Compile

```powershell
akernel skill compile examples\skills\markdown\context_budget.md
akernel skill compile examples\skills\markdown\context_budget.md --register
akernel skill compile examples\skills\markdown\context_budget.md --provider openai --model gpt-5.5
```

The default compiler is local and deterministic. Use `--provider openai` when the Markdown is less structured and needs model-assisted extraction.

## Validate

```powershell
akernel skill validate examples\skills\context_budget.json
```

Validation checks required fields and reports token estimates for `l0`, `l1`, `l2`, and `l3`.

## Inspect

```powershell
akernel skill inspect context_budget --budget 300
```

Inspection shows which progressive load level fits inside a budget.

## Supported Markdown Sections

- `Intent`
- `Inputs`
- `Outputs`
- `Constraints`
- `Failure Modes`
- `Procedure`
- `Examples`

Unknown sections are ignored in the first compiler. Unstructured introductory text is used as a fallback summary.
