---
name: filemaker-calc
description: |
  Use when the user wants to DEBUG, WRITE, or EXPLAIN a specific FileMaker
  calculation or custom function — "why does this calc return X", "write a calc
  that…", "explain what this formula does", evaluating/tracing a formula, or
  calculation-engine behavior questions (operator precedence, number display,
  type coercion, date math, JSON). Ships a bundled offline evaluator (fmeval.py)
  that RUNS calcs for real so you verify instead of guessing. For general FM Pro
  topics (scripting, layouts, relationships, plain function-reference lookups)
  use the filemaker-pro skill instead.
---

# FileMaker Calculation Engine — debug, write, explain

> This skill teaches you to reason about FileMaker calculations **correctly**, and
> gives you a tool to **verify** rather than guess.

FileMaker's calc docs (the `filemaker-pro` skill) teach *syntax* — function
signatures and descriptions. They barely teach *semantics*: precedence, coercion,
number display, type propagation, date math. Those are exactly where calculations
go wrong, and where an LLM's intuition (built from other languages) is usually
wrong. Don't trust your mental model of FileMaker — it lies. Run the calc.

## RULE ZERO — verify with the evaluator, don't guess

A bundled, dependency-free Python evaluator (`fmeval.py`, stdlib only) computes the
result the real FileMaker engine would, for the **pure** subset of the language
(functions of their inputs: text, number, logic, JSON, date/time/timestamp). It was
calibrated digit-for-digit against a live FileMaker. Use it the moment a question
turns on *what a calc actually returns*:

```
python <this-skill-dir>/fmeval.py '<calc expression>'
```

Examples (run them — the outputs below are real):

```
python fmeval.py 'Left ( "apples" ; 1 )'                  => a
python fmeval.py '1 / 3'                                  => .3333333333333333
python fmeval.py 'Date ( 2 ; 5 ; 2020 ) + 1'             => 2/6/2020
python fmeval.py 'JSONGetElement ( "{\"a\":[1,2,3]}" ; "a[1]" )'  => 2
```

### The modes

| Mode | Command | Use for                                                               |
|---|---|-----------------------------------------------------------------------|
| Evaluate | `fmeval.py '<calc>'` | What does this return?                                                |
| Parameters | `fmeval.py --param Name=value --param N2=v2 '<calc>'` | Test a custom-function **body** with inputs (repeatable)              |
| Trace | `fmeval.py --trace '<calc>'` | **Debug**: print every `Let`/`While` binding's value, then the result |
| Trace loop | `fmeval.py --trace-every N '<calc>'` | Trace a long `While` - init bindings + only every Nth iteration       |
| Test | `fmeval.py --test cases.tsv` | **Verify**: a TSV of `expr⇥expected` lines → pass/fail                |

`--trace` is the debugging superpower — it prints each `Let` binding, and for a
`While` loop every binding of every iteration (with `-- iteration N --` markers):

```
$ python fmeval.py --trace 'Let ( [ a = 5 ; b = a * 2 ] ; a + b )'
  a = 5
  b = 10
=> 15
```

For a long `While`, use `--trace-every N` to sample instead of drowning in output —
it shows the init bindings and then iterations 1, N, 2N, …:

```
$ python fmeval.py --trace-every 5 'While ( [ i = 0 ; s = 0 ] ; i < 20 ; [ i = i + 1 ; s = s + i ] ; s )'
  i = 0
  s = 0
  -- iteration 1 --
  i = 1
  s = 1
  -- iteration 5 --
  … (5, 10, 15, 20) …
=> 210
```

To debug a custom function, paste its **body** and pass the parameters:

```
python fmeval.py --param Number="555-1234" '<the function body that uses Number>'
```

On anything it can't evaluate, it prints `unsupported: <reason>` (see
[Coverage boundary](#coverage-boundary)) — never a wrong guess.

## Semantics cheat-sheet (the things the docs don't make obvious)

These were all confirmed against a live FileMaker. They are the usual sources of
"that's not what I expected."

### Operators & precedence
Tightest → loosest: `not` · `^` · `* /` · `+ -` · `&` · comparisons (`= ≠ <> < ≤ > ≥`)
· `and` · `or` `xor`. Two traps:
- **`not` binds *higher* than `^`** (so `not 1 + 1` = `(not 1) + 1` = `1`).
- **`&` binds *tighter* than comparisons** — so `a & "x" < b` parses as `(a & "x") < b`.
  Parenthesize comparisons inside concatenations.
- `=` is FileMaker's equality (there is no `==`). Equality/comparison return `1`/`0`.

### Comparison & coercion
- `=` and the ordering operators compare **numerically only when *both* operands are
  numbers**; if either is text, both are compared as **case-insensitive text**.
  So `0 = ""` is **false** (`"0" ≠ ""`), `"1.0" = "1"` is **false**, but `"A" = "a"`
  is **true** and `5 = "5"` is true. (`Exact()` is the case-*sensitive* text compare.)
- Text comparison is lexical: `"10" < "9"` is **true** (`"1"` < `"9"`).
- **`GetAsNumber`** keeps a leading sign, the first decimal point, all digit runs, AND
  scientific notation, stripping the rest: `"$1,234.56"` → `1234.56`, `"a1b2"` → `12`,
  `"1E3"` → `1000`.
- Truthiness (`If`/`Case`/`and`/`or`): non-zero numeric value is true; `""` and
  non-numeric text are false.

### Number display
FileMaker tags a value **inexact** if it came from a lossy op (`÷`, roots, `Pi`,
`Ln`/`Exp`/`Log`, non-integer `^`); inexactness **propagates** through arithmetic.
- **Inexact** values display **rounded to 16 decimal places**: `1 / 3` → `.3333333333333333`.
- **Exact** values (literals, `Random`, `GetAsNumber`, integer powers, `+ - ×` of
  exacts) display **in full**: a 22-digit literal shows all 22 digits; `2 ^ 64` is exact.
- Propagation example: `1 / 3 * 3` → **`1`** (the inexact `.999…` rounds back up).
- Always: trailing fractional zeros are trimmed, and the **leading zero is omitted**
  for magnitudes below 1 — FileMaker shows `.5`, not `0.5`.

### Text functions
- `Left/Middle/Right` positions are **1-based**; out-of-range counts clamp, don't error.
- `Position ( text ; search ; start ; occurrence )` — **case-insensitive**; 0 if not
  found; negative `occurrence` scans backward from `start` (use `start = Length(text)`
  to find the last match).
- `Substitute` is **case-sensitive**, applies pairs **sequentially** (`[a;b];[b;c]` does
  a→b *then* b→c on the result), and an **empty search returns the text unchanged**.
- `Replace ( text ; start ; count ; new )` — count 0 = pure insert; start past the end
  appends; start < 1 acts as 1; count clamps to what remains.
- `Filter` is case-sensitive and keeps characters in **text order**, not filter order.
- **Word boundaries**: hyphen, underscore, `=`, `&` start a new word; an apostrophe,
  period, or comma stays *inside* a word when between two word chars — so `it's`,
  `3.14`, `1,000.50`, `U.S.A` are each one word; `a-b-c` is three.
- **`Char`/`Code` are multi-character codecs**: `Code("ab")` = `97 + 98×100000` =
  `9800097`; each char occupies a 5-digit field, char[0] in the least-significant
  digits. **`Char(0)` is an empty string** (FileMaker strings are null-terminated), so
  `Char(40) & Char(0) & Char(41)` is `"()"`.

### ¶-delimited value lists
A "value" is text between `¶` (carriage returns). `GetValue` is 1-based; `ValueCount`
ignores a single trailing `¶`; `LeftValues`/`RightValues`/`MiddleValues` each append a
trailing `¶` to every value they return.

### Let / If / Case
- In a `Let` binding the **first `=` is the assignment** and binds loosest — the whole
  rest of the line is the value, even when it contains `and`/`or`/`=`
  (`z = Filter(x;"0") = Filter(x;"1")` assigns the comparison to `z`).
- Bindings see earlier bindings (and the function's parameters).
- `If` and `Case` are **lazy** — only the taken branch is evaluated (this is what lets
  recursive custom functions terminate). `Case` with no match and no default → empty;
  an odd trailing argument is the default.

### Date / Time / Timestamp (EN_US)
- These are numbers with a display type. **date = day count** where `1 = 0001-01-01`
  (= Python `date.toordinal()`); **time = seconds**; **timestamp = (day−1)×86400 +
  seconds**.
- Display: date `M/D/YYYY` (year zero-padded to 4 digits, e.g. `1/1/0001`); standalone
  time `H:MM:SS` (24-hour); timestamp `M/D/YYYY h:MM:SS AM/PM` (**12-hour**, and it
  **omits `:SS` when seconds are 0**).
- `DayOfWeek`: Sunday = 1 … Saturday = 7.
- **Arithmetic propagates the type**: `Date(…) + 1` is still a date; `Date − Date` is a
  plain number (a span of days).
- `Date(m;d;y)` **normalizes overflow**: `Date(13;1;2020)` = `1/1/2021`.
- A **2-digit year** uses a sliding window `[currentYear−69, currentYear+30]` — so in
  2026, `50` → 2050 but `60` → 1960. (`GetAsDate` of a number treats it as a raw day
  count; of text it parses `M/D/YYYY` strictly.)

### JSON
Object keys are emitted **sorted alphabetically**; array order is preserved; compact
output has **no spaces** (`{"a":11,"b":22.23}`). A valid document's root must be an
object or array (a bare scalar is invalid). Errors return ordinary **text starting with
`"?"`** (they do *not* abort the calc). `JSONGetElement` of a missing key or a `null`
returns **empty**; numbers/booleans come back as numbers. Type constants:
`JSONString`=1, `JSONNumber`=2, `JSONObject`=3, `JSONArray`=4, `JSONBoolean`=5,
`JSONNull`=6, `JSONRaw`=0.

## Playbooks

### Debug a calc ("why does this return X / the wrong thing?")
1. Reproduce: run it with `--param` for the real inputs (or as a literal expression).
2. If it's a `Let`, run `--trace` and read down the bindings to the **first one that's
   wrong** — that localizes the bug far faster than re-reading the formula.
3. Form a hypothesis using the cheat-sheet (precedence? coercion? a `Substitute`
   case-sensitivity? a `Position` that returned 0?), then **test the hypothesis** by
   evaluating the sub-expression in isolation.
4. Propose the fix and **re-run** to confirm; show the before/after evaluation.

### Write a calc
1. Draft it.
2. Write `expr⇥expected` cases covering the normal path and the edges (empty input,
   boundaries, the gotchas above) into a `.tsv`.
3. `fmeval.py --test cases.tsv` and iterate until green.
4. Present the calc **with the verified test table** so the user can see it was checked,
   not just asserted.

### Explain a calc
1. Evaluate it on a few representative inputs so your explanation is grounded in real
   output, not assumed behavior.
2. Use `--trace` to narrate what each `Let` step computes.
3. Call out any of the cheat-sheet quirks the calc depends on (that's usually the part
   the user actually needed explained).

## Coverage boundary

The evaluator is authoritative for **pure functions of the inputs** — most calcs and
most custom functions. It is **not** authoritative for runtime context, and reports
`unsupported: <reason>` for:

- field references (`Table::Field`), `Get(...)` runtime state
- `ExecuteSQL`, `GetNthRecord`, `GetSummary`, related/aggregate-over-records functions
- container functions, plug-in (external) functions
- `Evaluate` of a non-literal expression
- non-EN_US locales for date/time formatting

When you cross that line: stop verifying, reason from the function reference, and
**flag the uncertainty explicitly** ("I can't execute this part — it depends on a field
value; based on the docs it should…"). Never present an unverified result as if the
evaluator confirmed it.

## Function reference (on demand)

For exact signatures, parameters, and Claris's own worked examples, use the
**filemaker-pro** skill's function navigator (`pro-func-*.md`), or fetch the page
directly — Claris publishes one markdown file per function at a predictable URL:

```
https://help.claris.com/markdown/en/pro-help/<function-name-lowercase>.md
```

e.g. `position.md`, `substitute.md`, `jsonsetelement.md`, `let.md`. Pull these when you
need a detail the cheat-sheet doesn't cover; verify behavior with the evaluator.
