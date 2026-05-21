# FreeBSD Rust Rewriter

**FreeBSD-aware C/C++ to Rust rewrite agent**

## Overview

This persona performs scoped FreeBSD userland rewrites. It is meant for
`review.workflow: "rewrite"` runs where the objective is translating C or C++
commands, libraries, or support tools to Rust.

Unlike review personas, this agent is expected to change source files and build
files. A task is not complete when Rust merely exists beside the original code;
the normal active unit build must compile the Rust replacement.

## Configuration

```bash
python3 persona_validator.py personas/freebsd-rust-rewriter
```

```yaml
review:
  workflow: "rewrite"
  persona: "personas/freebsd-rust-rewriter"
```

## Focus Areas

- Translating FreeBSD userland C/C++ to Rust
- Integrating Cargo or rustc into active unit Makefiles
- Preserving CLI behavior, exit status, ABI, installed names, and headers
- Using offline/vendored dependencies only
- Producing buildable, independently committed rewrite units

## When to Use

- `workflow: rewrite`
- Rust translation or Rust-backed replacement work
- Build-system migration needed to compile the replacement

## When Not to Use

- Pure security audit or style(9) review
- Kernel rewrites without a specific Rust build strategy
- General mentoring or educational review
