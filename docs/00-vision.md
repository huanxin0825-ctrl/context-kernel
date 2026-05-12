# Vision

## Problem

Modern agent systems spend too much context on repeated operating instructions, oversized tool descriptions, whole skill documents, compressed conversation history, and stale memory. The result is expensive, brittle, and often less intelligent than the underlying model should be.

The root issue is architectural: the prompt is being used as a runtime, a memory layer, a policy layer, and a tool protocol at the same time.

## Mission

Build a context-native agent runtime that lets models receive only the information needed for the current reasoning step.

The framework should make agent work cheaper, sharper, and less narrow by moving mechanical responsibilities out of natural-language prompts and into runtime systems.

## Non-Negotiable Principles

- Context is a working set, not a warehouse.
- Memory is structured state, not compressed chat logs.
- Skills are executable contracts, not long Markdown manuals.
- Policies should be enforced by runtime where possible.
- Token spending must be auditable.
- Smaller context must be measured against task success, not assumed.
- The first version must stay simple enough to inspect and improve.

## Success Definition

The first credible version should show that a CLI agent can complete representative tasks while using substantially less input context than a long-prompt baseline.

Target for the first public proof:

- 50% or greater reduction in input token estimate on benchmark tasks.
- Equal or better task success on simple workflow tasks.
- Trace output that explains every context inclusion.
- Structured memory retrieval that avoids full-history prompt stuffing.

## What This Is Not

Context Kernel is not trying to replace every agent framework in its first release. It is a runtime experiment focused on context discipline, memory structure, skill contracts, and token observability.

