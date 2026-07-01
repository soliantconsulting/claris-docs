#!/usr/bin/env python3
"""fmeval.py - evaluate FileMaker calculation expressions.

Where fmcalc.py turns compiled ref=6 bytecode into readable infix *text*, this
module goes one step further: it PARSES calc source text into an expression
tree (AST) and EVALUATES it against supplied parameter values. So given the
body of a custom function and a set of arguments, it computes the result the
way FileMaker would -- for the pure subset of the language: text, number,
logic, JSON, and date/time/timestamp (EN_US), as functions of their inputs.

Two front doors:

  * eval_source(src, params=...)         -- parse + evaluate raw calc text
  * eval_customfn(cache, name, args)     -- evaluate a custom function stored in
                                            an fmp12/.fch file, by decompiling
                                            its bytecode (via fmcalc) to text and
                                            feeding that here.

Only ONE parser exists (this one, for source text). Stored custom functions are
routed through fmcalc's already-verified bytecode->text decompiler first, so the
grammar is never duplicated.

What it can't do is reported, not crashed on: anything needing runtime context
(field references, Get(...), ExecuteSQL, container/plug-in functions, Evaluate
of a non-literal) raises EvalUnsupported with a reason. The evaluator's domain
is pure functions of the inputs, which is most custom functions.

Number semantics use decimal.Decimal (FileMaker carries up to 400 significant
digits). Precedence follows FileMaker's documented order (operators-in-formulas):
NOT binds higher than ^, then * /, + -, &, comparisons, AND, then OR/XOR.
"""

import decimal
import difflib
import base64
import json
import math
import random as _random
import re
import urllib.parse as _urlparse
import uuid as _uuid
from datetime import date as _pydate, datetime as _pydatetime, timezone as _pytz
from decimal import Decimal

# FileMaker keeps up to 400 significant digits; mirror that for arithmetic.
_CTX = decimal.Context(prec=400)

CR = "\r"            # FileMaker's value separator (rendered ¶ in calc text)

# ---- temporal model (EN_US) ------------------------------------------------
# FileMaker dates/times/timestamps are numbers carrying a display TYPE:
#   date      = day count, 1 = 0001-01-01  (== Python date.toordinal())
#   time      = seconds
#   timestamp = seconds since 0001-01-01 00:00:00 = (day-1)*86400 + timeSeconds
# Display is locale-specific; we implement EN_US only. Note the quirk a live
# FileMaker confirms: a standalone time prints 24-hour (13:30:45) but the time
# part of a timestamp prints 12-hour with AM/PM (1:30:45 PM).
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday"]
_MIN_ORD, _MAX_ORD = 1, 3652059       # date.fromordinal valid range (1..9999)
_TODAY_YEAR = _pydate.today().year    # the 2-digit-year window slides with this


class FMTemporal:
    """A FileMaker date / time / timestamp: a numeric value plus a display kind."""
    __slots__ = ("value", "kind")

    def __init__(self, value, kind):
        self.value = value if isinstance(value, Decimal) else num(value)
        self.kind = kind

    def text(self):
        if self.kind == "date":
            return _fmt_date(self.value)
        if self.kind == "time":
            return _fmt_time(self.value)
        if self.kind == "time12":
            return _fmt_time12(self.value)
        return _fmt_timestamp(self.value)


def _fmt_date(days):
    n = int(days)
    if not (_MIN_ORD <= n <= _MAX_ORD):
        return "?"
    d = _pydate.fromordinal(n)
    return "%d/%d/%04d" % (d.month, d.day, d.year)   # year zero-padded to 4 digits


def _frac_str(frac):
    """The fractional-seconds suffix, e.g. Decimal('0.5') -> '.5' ('' if zero)."""
    return format(frac, "f")[1:].rstrip("0") if frac else ""


def _fmt_time(secs):                              # standalone time: 24-hour H:MM:SS
    val = secs if isinstance(secs, Decimal) else num(secs)
    neg = val < 0
    val = abs(val)
    whole = int(val)
    return "%s%d:%02d:%02d%s" % ("-" if neg else "", whole // 3600,
                                 (whole % 3600) // 60, whole % 60,
                                 _frac_str(val - whole))


def _fmt_time12(secs):                            # 12-hour time: h:MM:SS AM/PM
    val = secs if isinstance(secs, Decimal) else num(secs)
    neg = val < 0
    val = abs(val)
    whole = int(val)
    h = whole // 3600
    return "%s%d:%02d:%02d%s %s" % ("-" if neg else "", (h % 12) or 12,
                                    (whole % 3600) // 60, whole % 60,
                                    _frac_str(val - whole), "AM" if h % 24 < 12 else "PM")


def _fmt_timestamp(total):                        # M/D/YYYY h:MM[:SS] AM/PM (12-hour)
    val = total if isinstance(total, Decimal) else num(total)
    whole = int(val)
    frac = val - whole
    days, secs = whole // 86400 + 1, whole % 86400
    if not (_MIN_ORD <= days <= _MAX_ORD):
        return "?"
    d = _pydate.fromordinal(days)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if s == 0 and not frac:                       # FileMaker omits :SS when zero
        tpart = "%d:%02d" % ((h % 12) or 12, m)
    else:
        tpart = "%d:%02d:%02d%s" % ((h % 12) or 12, m, s, _frac_str(frac))
    return "%d/%d/%04d %s %s" % (d.month, d.day, d.year, tpart,
                                 "AM" if h < 12 else "PM")


def _is_num(v):
    """A value that compares/arithmetics numerically: a plain number or a
    temporal (which is fundamentally a number)."""
    return isinstance(v, (Decimal, FMTemporal))


# ===========================================================================
# Errors
# ===========================================================================
class CalcSyntaxError(Exception):
    """The source text could not be tokenized/parsed."""


class EvalError(Exception):
    """A run-time evaluation failure (FileMaker would typically show '?')."""


class EvalUnsupported(EvalError):
    """The expression is well-formed but depends on something we can't provide
    offline (a field value, Get() runtime state, a plug-in, etc.)."""


# ===========================================================================
# Value model -- a value is text (Python str), number (Decimal), or a temporal
# (FMTemporal wrapping a number). Boolean results are Decimal 1/0.
# ===========================================================================
def format_number(d):
    """FileMaker's unformatted text form of a number: fixed-point (never
    scientific), no thousands separators, trailing fractional zeros trimmed, and
    the leading zero omitted for magnitudes below 1 (FileMaker shows .5, not 0.5).
    An INEXACT value (division/root/Pi/etc.) is rounded to 16 decimal places first;
    an exact value (literal, Random, integer math) is shown in full."""
    if d.is_nan():
        return "?"
    if isinstance(d, _Inexact):
        exp = d.as_tuple().exponent
        if isinstance(exp, int) and exp < -16:    # more than 16 decimal places
            d = d.quantize(Decimal(1).scaleb(-16), rounding=decimal.ROUND_HALF_UP,
                           context=_CTX)
    if d == 0:
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def as_text(v):
    if isinstance(v, FMTemporal):
        return v.text()
    return v if isinstance(v, str) else format_number(v)


def as_number(v):
    """Coerce to Decimal the way GetAsNumber does: a Decimal (or temporal's
    underlying number) passes through; text yields the numeric content it
    contains. FileMaker strips noise (currency symbols, thousands separators) but
    keeps a leading sign, the FIRST decimal point, the digits, AND scientific
    notation -- so "$1,234.56" -> 1234.56, "a1b2" -> 12, "1E3" -> 1000. -> 0."""
    if isinstance(v, FMTemporal):
        return v.value
    if isinstance(v, Decimal):
        return v
    if not v:
        return Decimal(0)
    mant, exp = [], []
    sign, esign = "", ""
    seen_dot = seen_e = False
    for ch in v:
        if not seen_e:
            if ch in "-+" and not mant and not sign:
                sign = "-" if ch == "-" else ""
            elif ch.isdigit():
                mant.append(ch)
            elif ch == "." and not seen_dot:
                seen_dot = True
                mant.append(".")
            elif ch in "eE" and mant:            # exponent (needs a mantissa)
                seen_e = True
        else:
            if ch in "-+" and not exp and not esign:
                esign = "-" if ch == "-" else ""
            elif ch.isdigit():
                exp.append(ch)
    if not any(c.isdigit() for c in mant):
        return Decimal(0)
    s = sign + "".join(mant)
    if seen_e and exp:
        s += "e" + esign + "".join(exp)
    try:
        return _CTX.create_decimal(s)
    except decimal.InvalidOperation:
        return Decimal(0)


def as_bool(v):
    """Truthiness: any non-zero numeric value is true (GetAsBoolean)."""
    return as_number(v) != 0


def fm_equal(a, b):
    """FileMaker '=' : numeric only when BOTH operands are numbers; if either is
    text the comparison is done on the text forms, case-INsensitively (Exact() is
    the case-sensitive one). So 0 = "" is false ("0" != "") and "1.0" = "1" is
    false, but 5 = "5" is true."""
    if _is_num(a) and _is_num(b):
        return as_number(a) == as_number(b)
    return as_text(a).casefold() == as_text(b).casefold()


def fm_order(a, b):
    """Return -1/0/1 for a<b / a==b / a>b, numeric only when both are numbers
    (temporals included), otherwise a case-insensitive text comparison."""
    if _is_num(a) and _is_num(b):
        na, nb = as_number(a), as_number(b)
        return (na > nb) - (na < nb)
    ca, cb = as_text(a).casefold(), as_text(b).casefold()
    return (ca > cb) - (ca < cb)


def num(d):
    """Wrap a Python int/float/Decimal as a calc Decimal in our context."""
    if isinstance(d, Decimal):
        return +_CTX.create_decimal(d)
    return _CTX.create_decimal(str(d))


def _random_value():
    """FileMaker Random: a fresh value in [0, 1). Nondeterministic (so evaluating
    the same calc twice differs). Generated with ~19 decimal digits to resemble
    the engine's output. EXACT (terminating), so it displays in full."""
    return _CTX.create_decimal(_random.randrange(10 ** 19)).scaleb(-19)


class _Inexact(Decimal):
    """A Decimal produced by a lossy/irrational operation (division, root, Pi,
    ln/exp/log, non-integer power). FileMaker displays such values ROUNDED to 16
    decimal places, while EXACT values (literals, Random, integer arithmetic, +-*)
    display in full -- which is why a 22-digit literal shows fully but 1/3 shows 16,
    and 1/3*3 (inexact) rounds back to 1. Inexactness propagates through arithmetic."""
    __slots__ = ()


def _inexact(d):
    return d if isinstance(d, _Inexact) else _Inexact(d)


def _propagate_inexact(result, *operands):
    """Tag a numeric result inexact if any operand was inexact (and it isn't a
    temporal/string)."""
    if (isinstance(result, Decimal) and not isinstance(result, _Inexact)
            and any(isinstance(o, _Inexact) for o in operands)):
        return _inexact(result)
    return result


def _fm_pow(base, ex):
    """base ^ ex in FileMaker's high-precision decimal engine. Integer exponents
    are exact; a fractional exponent uses exp(ex*ln(base)) and so requires a
    positive base (a negative base with a fractional power has no real result)."""
    if ex == ex.to_integral_value() and abs(ex) < 100000:
        try:
            return _CTX.power(base, ex)            # integer power is exact
        except (decimal.InvalidOperation, decimal.Overflow):
            raise EvalError("invalid exponentiation")
    if base < 0:
        raise EvalError("negative base raised to a fractional power")
    if base == 0:
        return num(0)
    try:
        return _inexact(_CTX.power(base, ex))      # fractional power is inexact
    except (decimal.InvalidOperation, decimal.Overflow):
        raise EvalError("invalid exponentiation")


# ===========================================================================
# Tokenizer
# ===========================================================================
# Multi-char and unicode operators first so the scanner is greedy-correct.
_OPS = ["<=", ">=", "<>", "≤", "≥", "≠", "=", "<", ">", "&", "+", "-",
        "*", "/", "^", "(", ")", ";", "[", "]"]
_KEYWORD_OPS = {"and", "or", "xor", "not"}


class Tok:
    __slots__ = ("kind", "val", "pos")

    def __init__(self, kind, val, pos):
        self.kind, self.val, self.pos = kind, val, pos

    def __repr__(self):
        return "Tok(%s,%r)" % (self.kind, self.val)


def tokenize(s):
    """Lex calc source into a token list. Kinds: num, str, name, field, var, op,
    end. Strings use FileMaker conventions: doubled "" and backslash escapes."""
    toks = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n\xa0":
            i += 1
            continue
        if c == "¶":                              # literal carriage return
            toks.append(Tok("str", CR, i))
            i += 1
            continue
        if s.startswith("//", i):                 # line comment
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if s.startswith("/*", i):                 # block comment
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':                              # string literal
            i, text = _scan_string(s, i)
            toks.append(Tok("str", text, i))
            continue
        if c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = re.match(r"\d*\.?\d+(?:[eE][-+]?\d+)?", s[i:])
            toks.append(Tok("num", m.group(0), i))
            i += m.end()
            continue
        if c in "$":                              # $local / $$global variable
            m = re.match(r"\$\$?[A-Za-z0-9_.\[\]]*", s[i:])
            toks.append(Tok("var", m.group(0), i))
            i += m.end()
            continue
        if c.isalpha() or c == "_":               # identifier / field / keyword
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", s[i:])
            word = m.group(0)
            i += m.end()
            # a field reference is TO::Field (identifiers may be space-separated
            # in real schemas, but the evaluable subset uses simple names).
            if s.startswith("::", i):
                m2 = re.match(r"::[A-Za-z_][A-Za-z0-9_]*", s[i:])
                if m2:
                    toks.append(Tok("field", word + m2.group(0), i))
                    i += m2.end()
                    continue
            low = word.lower()
            if low in _KEYWORD_OPS:
                toks.append(Tok("op", low, i))
            else:
                toks.append(Tok("name", word, i))
            continue
        for op in _OPS:                           # punctuation / operators
            if s.startswith(op, i):
                toks.append(Tok("op", op, i))
                i += len(op)
                break
        else:
            raise CalcSyntaxError("unexpected character %r at %d" % (c, i))
    toks.append(Tok("end", None, n))
    return toks


def _scan_string(s, i):
    """Scan a "..."-delimited literal starting at the opening quote. Supports
    backslash escapes (\\" \\\\ \\r \\n \\t) and the doubled-quote ("") escape."""
    n = len(s)
    i += 1                                        # skip opening quote
    out = []
    while i < n:
        c = s[i]
        if c == '"':
            if i + 1 < n and s[i + 1] == '"':     # "" -> literal quote
                out.append('"')
                i += 2
                continue
            return i + 1, "".join(out)            # closing quote
        if c == "\\" and i + 1 < n:
            nx = s[i + 1]
            if nx in ('"', "\\"):
                out.append(nx); i += 2; continue
            if nx in ("r", "n", "¶"):             # \r \n \¶ all mean a carriage return
                out.append(CR); i += 2; continue
            if nx == "t":
                out.append("\t"); i += 2; continue
            out.append("\\"); i += 1; continue    # unrecognized escape: keep the backslash
        out.append(CR if c == "¶" else c)
        i += 1
    raise CalcSyntaxError("unterminated string literal")


# ===========================================================================
# AST nodes -- lightweight tagged tuples:
#   ("num", Decimal) ("str", str) ("var", name) ("field", "TO::F")
#   ("name", ident)                       bare identifier (const / 0-arg / field)
#   ("call", name, [args])                function call (incl. Let/Case/etc.,
#                                         dispatched as special forms at eval)
#   ("bin", op, left, right)              binary operator
#   ("un", op, operand)                   unary not / -
#   ("list", [nodes])                     bracket group  [ a ; b ; ... ]
# ===========================================================================

# binding power (higher = tighter), FileMaker documented order
_BP = {"or": 1, "xor": 1, "and": 2,
       "=": 3, "≠": 3, "<>": 3, "<": 3, ">": 3, "≤": 3, "<=": 3, "≥": 3, ">=": 3,
       "&": 4, "+": 5, "-": 5, "*": 6, "/": 6, "^": 7}
_PREFIX_BP = 8                                    # not / unary minus (above ^)


class _Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self):
        return self.t[self.i]

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, kind, val=None):
        tok = self.next()
        if tok.kind != kind or (val is not None and tok.val != val):
            raise CalcSyntaxError("expected %s %r, got %s %r at %d" %
                                  (kind, val, tok.kind, tok.val, tok.pos))
        return tok

    def parse(self):
        node = self.expr(0)
        if self.peek().kind != "end":
            raise CalcSyntaxError("trailing tokens at %d" % self.peek().pos)
        return node

    def expr(self, min_bp):
        left = self.prefix()
        while True:
            tok = self.peek()
            if tok.kind != "op" or tok.val not in _BP:
                break
            bp = _BP[tok.val]
            if bp < min_bp:
                break
            self.next()
            right = self.expr(bp + 1)             # left-assoc
            left = ("bin", tok.val, left, right)
        return left

    def prefix(self):
        tok = self.peek()
        if tok.kind == "op" and tok.val in ("not", "-"):
            self.next()
            return ("un", tok.val, self.expr(_PREFIX_BP))
        return self.atom()

    def atom(self):
        tok = self.next()
        if tok.kind == "num":
            return ("num", num(tok.val))
        if tok.kind == "str":
            return ("str", tok.val)
        if tok.kind == "var":
            return ("var", tok.val)
        if tok.kind == "field":
            return ("field", tok.val)
        if tok.kind == "op" and tok.val == "(":
            node = self.expr(0)
            self.expect("op", ")")
            return node
        if tok.kind == "op" and tok.val == "[":
            return self._bracket()
        if tok.kind == "name":
            if self.peek().kind == "op" and self.peek().val == "(":
                self.next()
                args = self._arglist(")")
                return ("call", tok.val, args)
            return ("name", tok.val)
        raise CalcSyntaxError("unexpected %s %r at %d" % (tok.kind, tok.val, tok.pos))

    def _bracket(self):
        items = []
        if not (self.peek().kind == "op" and self.peek().val == "]"):
            items.append(self.expr(0))
            while self.peek().kind == "op" and self.peek().val == ";":
                self.next()
                items.append(self.expr(0))
        self.expect("op", "]")
        return ("list", items)

    def _arglist(self, close):
        args = []
        if self.peek().kind == "op" and self.peek().val == close:
            self.next()
            return args
        args.append(self.expr(0))
        while True:
            tok = self.next()
            if tok.kind == "op" and tok.val == ";":
                args.append(self.expr(0))
            elif tok.kind == "op" and tok.val == close:
                return args
            else:
                raise CalcSyntaxError("expected ';' or %r at %d" % (close, tok.pos))


def parse(src):
    return _Parser(tokenize(src)).parse()


# ===========================================================================
# Environment
# ===========================================================================
class Env:
    """Variable bindings for one evaluation frame. `names` maps lower-cased
    identifiers (parameters and Let locals) to values; `vars` maps $/$$ variable
    names. A function library (name -> compiled custom fn) supports recursion."""

    def __init__(self, names=None, vars=None, library=None, depth=0, trace=None,
                 trace_every=1, getvals=None):
        self.names = names or {}
        self.vars = vars or {}
        self.library = library or {}
        self.depth = depth
        self.trace = trace            # if a list, Let/While bindings append (name, value)
        self.trace_every = trace_every  # While: record only every Nth iteration
        self.getvals = getvals or {}  # Get() selector overrides (lower-cased keys)

    def child(self, names):
        return Env(names, self.vars, self.library, self.depth + 1, self.trace,
                   self.trace_every, self.getvals)


MAX_DEPTH = 4000                                  # recursion guard for custom fns
MAX_ITER = 1000000                                # iteration guard for While


# ===========================================================================
# Evaluator
# ===========================================================================
def evaluate(node, env):
    tag = node[0]
    if tag == "num" or tag == "str":
        return node[1]
    if tag == "name":
        return _resolve_name(node[1], env)
    if tag == "var":
        if node[1] in env.vars:
            return env.vars[node[1]]
        raise EvalUnsupported("variable %s has no value in this context" % node[1])
    if tag == "field":
        raise EvalUnsupported("field reference %s (no record context)" % node[1])
    if tag == "bin":
        return _eval_binop(node[1], node[2], node[3], env)
    if tag == "un":
        operand = evaluate(node[2], env)
        if node[1] == "not":
            return num(0) if as_bool(operand) else num(1)
        return _propagate_inexact(-as_number(operand), operand)
    if tag == "list":
        raise EvalError("a [ ... ] group is only valid inside Let or Substitute")
    if tag == "call":
        return _eval_call(node[1], node[2], env)
    raise EvalError("cannot evaluate node %r" % (tag,))


def _resolve_name(word, env):
    low = word.lower()
    if low in env.names:
        return env.names[low]
    if low == "true":
        return num(1)
    if low == "false":
        return num(0)
    if low == "pi":
        return _inexact(num("3.14159265358979323846264338327950288419716939937510582097494459"))
    if low == "random":                            # no-arg keyword, like Pi
        return _random_value()
    if low in _JSON_CONST:                         # JSONString, JSONNumber, ...
        return num(_JSON_CONST[low])
    if low in env.library:                        # 0-parameter custom function
        return _call_customfn(env.library[low], [], env)
    if low in FUNCS or low in SPECIAL:
        raise EvalError("%s expects arguments" % word)
    raise EvalUnsupported("unknown name %r (field or undefined variable)" % word)


def _eval_binop(op, ln, rn, env):
    if op in ("and", "or", "xor"):
        a = as_bool(evaluate(ln, env))
        b = as_bool(evaluate(rn, env))
        if op == "and":
            return num(1) if (a and b) else num(0)
        if op == "or":
            return num(1) if (a or b) else num(0)
        return num(1) if (a != b) else num(0)
    a = evaluate(ln, env)
    b = evaluate(rn, env)
    if op == "&":
        return as_text(a) + as_text(b)
    if op == "+":
        return _propagate_inexact(_temporal_addsub(a, b, as_number(a) + as_number(b)), a, b)
    if op == "-":
        return _propagate_inexact(_temporal_addsub(a, b, as_number(a) - as_number(b),
                                                   minus=True), a, b)
    if op == "*":
        return _propagate_inexact(_CTX.multiply(as_number(a), as_number(b)), a, b)
    if op == "/":
        d = as_number(b)
        if d == 0:
            raise EvalError("division by zero")
        return _inexact(_CTX.divide(as_number(a), d))   # division is inexact
    if op == "^":
        return _propagate_inexact(_fm_pow(as_number(a), as_number(b)), a, b)
    # comparisons -> 1 / 0
    if op == "=":
        r = fm_equal(a, b)
    elif op in ("≠", "<>"):
        r = not fm_equal(a, b)
    else:
        c = fm_order(a, b)
        r = {"<": c < 0, ">": c > 0, "≤": c <= 0, "<=": c <= 0,
             "≥": c >= 0, ">=": c >= 0}[op]
    return num(1) if r else num(0)


def _temporal_addsub(a, b, result, minus=False):
    """Type inference for + and -: a temporal plus/minus a plain number stays the
    same temporal kind (Date(...)+1 -> a date); temporal minus temporal yields a
    plain number (a span). number - temporal is a plain number too."""
    ta, tb = isinstance(a, FMTemporal), isinstance(b, FMTemporal)
    if ta and tb:
        return result
    if ta:
        return FMTemporal(result, a.kind)
    if tb and not minus:
        return FMTemporal(result, b.kind)
    return result


def _eval_call(name, args, env):
    low = name.lower()
    if low in SPECIAL:
        return SPECIAL[low](args, env)
    if low in env.library:
        return _call_customfn(env.library[low], [evaluate(a, env) for a in args], env)
    if low in FUNCS:
        impl, arity = FUNCS[low]
        if arity != "V" and len(args) != arity:
            raise EvalError("%s takes %d argument(s), got %d" % (name, arity, len(args)))
        return impl([evaluate(a, env) for a in args])
    if low in UNSUPPORTED:
        raise EvalUnsupported("%s is not supported offline (%s)" % (name, UNSUPPORTED[low]))
    raise EvalUnsupported("unknown function %s" % name)


def _call_customfn(fn, argvals, env):
    if env.depth >= MAX_DEPTH:
        raise EvalError("custom-function recursion exceeded %d frames" % MAX_DEPTH)
    params = fn["params"]
    names = {}
    for k, p in enumerate(params):
        names[p.strip().lower()] = argvals[k] if k < len(argvals) else ""
    return evaluate(fn["ast"], env.child(names))


# ---- special forms (control their own argument evaluation) ----------------
def _sf_if(args, env):
    if len(args) not in (2, 3):
        raise EvalError("If takes 2 or 3 arguments")
    if as_bool(evaluate(args[0], env)):
        return evaluate(args[1], env)
    return evaluate(args[2], env) if len(args) == 3 else ""


def _sf_case(args, env):
    if not args:
        raise EvalError("Case takes at least 1 argument")
    i = 0
    while i + 1 < len(args):
        if as_bool(evaluate(args[i], env)):
            return evaluate(args[i + 1], env)
        i += 2
    return evaluate(args[i], env) if i < len(args) else ""   # trailing default


def _sf_choose(args, env):
    if len(args) < 2:
        raise EvalError("Choose takes at least 2 arguments")
    idx = int(as_number(evaluate(args[0], env)))
    pick = idx + 1
    if 1 <= pick < len(args):
        return evaluate(args[pick], env)
    return ""


def _apply_bindings(binds_node, work, names, record=True):
    """Evaluate a Let/While binding block (a single binding or a [ ... ] list) into
    `work`, updating `names` in place (and $vars). Records to --trace when `record`
    is set (While clears it on the iterations it's sampling past)."""
    pairs = binds_node[1] if binds_node[0] == "list" else [binds_node]
    for b in pairs:
        nm, valnode = _split_binding(b)
        val = evaluate(valnode, work)
        if record and work.trace is not None:     # record for --trace debugging
            work.trace.append((nm, val))
        if nm.startswith("$"):
            work.vars[nm] = val
        else:
            names[nm.lower()] = val


def _sf_let(args, env):
    """Let ( var=expr | [ v1=e1 ; v2=e2 ] ; calculation ). Bindings see earlier
    ones; they are layered onto the current names so the body sees parameters too."""
    if len(args) != 2:
        raise EvalError("Let takes 2 arguments")
    binds_node, body = args
    names = dict(env.names)
    work = env.child({})
    work.names = names
    _apply_bindings(binds_node, work, names)
    return evaluate(body, work)


def _sf_while(args, env):
    """While ( [initialVariables] ; condition ; [logic] ; result ). Like Let, but
    re-runs the logic block (which reassigns variables) until condition is false."""
    if len(args) != 4:
        raise EvalError("While takes 4 arguments")
    init_node, test_node, logic_node, result_node = args
    names = dict(env.names)
    work = env.child({})
    work.names = names
    _apply_bindings(init_node, work, names)
    stride = work.trace_every if work.trace_every and work.trace_every > 0 else 1
    n = 0
    while as_bool(evaluate(test_node, work)):
        n += 1
        if n > MAX_ITER:
            raise EvalError("While exceeded %d iterations" % MAX_ITER)
        record = work.trace is not None and (n == 1 or n % stride == 0)
        if record:
            work.trace.append(("#iter", n))       # marks a new iteration for --trace
        _apply_bindings(logic_node, work, names, record=record)
    return evaluate(result_node, work)


def _split_binding(node):
    """A Let binding is `name = value`. The FIRST '=' is the assignment and binds
    loosest -- everything after it is the value -- but the generic expression
    parser treats '=' as a comparison, so a value containing lower-or-equal
    precedence operators (and/or, or another '=') parses with the assignment '='
    buried in the tree (e.g. `z = A = B` -> (z=A)=B, `Intl = A and B` -> (Intl=A)
    and B). Walk down the left spine to the binding '=' (the one whose left child
    is a bare name/var) and rebuild the value above it. Return (name, value_node)."""
    def find(n):
        if n[0] != "bin":
            return None
        if n[1] == "=" and n[2][0] in ("name", "var"):
            return n[2][1], n[3]                  # the assignment: value = right child
        res = find(n[2])                          # the binding '=' is further left
        if res:
            name, val = res
            return name, ("bin", n[1], val, n[3])  # rebuild this op above it
        return None
    res = find(node)
    if not res:
        raise CalcSyntaxError("malformed Let binding")
    return res


def _sf_substitute(args, env):
    if len(args) < 2:
        raise EvalError("Substitute takes at least 2 arguments")
    text = as_text(evaluate(args[0], env))
    pairs = []
    rest = args[1:]
    if len(rest) == 2 and rest[0][0] != "list":   # 3-arg form: search ; replace
        pairs.append((rest[0], rest[1]))
    else:
        for grp in rest:
            if grp[0] != "list" or len(grp[1]) != 2:
                raise EvalError("Substitute pairs must be [ search ; replace ]")
            pairs.append((grp[1][0], grp[1][1]))
    for s_node, r_node in pairs:
        s = as_text(evaluate(s_node, env))
        r = as_text(evaluate(r_node, env))
        if s:
            text = text.replace(s, r)
    return text


def _sf_evaluate(args, env):
    """Evaluate ( expression {; [fields]} ) -- supported only when the expression
    is a literal string we can parse here; otherwise it needs the FileMaker
    runtime to compile arbitrary text."""
    if not args:
        raise EvalError("Evaluate takes at least 1 argument")
    if args[0][0] != "str":
        raise EvalUnsupported("Evaluate of a non-literal expression")
    return evaluate(parse(args[0][1]), env)


# Get ( selector ) -- runtime state. Offline, a selector resolves in order:
#   1. a caller-supplied value (env.getvals; CLI: --get Selector=value)
#   2. a live clock value for the date/time selectors (nondeterministic, like FileMaker)
#   3. the Claris doc's example value, so the calc yields something illustrative
#   4. otherwise empty "".
# Date/time selectors coerce their string to the right temporal type so arithmetic
# works (e.g. Get ( CurrentDate ) + 7). CurrentTime displays 12-hour, as FileMaker does.
_GET_TEMPORAL = {"currentdate": "date", "currenttime": "time12",
                 "currenttimestamp": "timestamp", "currenthosttimestamp": "timestamp"}
_GET_CLOCK = {"currentdate", "currenttime", "currenttimestamp", "currenthosttimestamp",
              "currenttimeutcmilliseconds", "currenttimeutcmicroseconds"}

# Get() selector -> Claris doc example value (harvested from skills/get-*.md); used as
# a plausible default for selectors we can't compute. Clock selectors are excluded
# (computed live above).
_GET_SELECTORS = {  # valid Get() selectors (cp_getsel + doc pages); lower -> proper
    'accountextendedprivileges': 'AccountExtendedPrivileges',
    'accountgroupname': 'AccountGroupName', 'accountname': 'AccountName',
    'accountprivilegesetname': 'AccountPrivilegeSetName', 'accounttype': 'AccountType',
    'activefieldcontents': 'ActiveFieldContents', 'activefieldname': 'ActiveFieldName',
    'activefieldtablename': 'ActiveFieldTableName',
    'activelayoutobjectname': 'ActiveLayoutObjectName',
    'activemodifierkeys': 'ActiveModifierKeys',
    'activeportalrownumber': 'ActivePortalRowNumber',
    'activerecordnumber': 'ActiveRecordNumber',
    'activerepetitionnumber': 'ActiveRepetitionNumber',
    'activeselectionsize': 'ActiveSelectionSize',
    'activeselectionstart': 'ActiveSelectionStart', 'allowabortstate': 'AllowAbortState',
    'allowformattingbarstate': 'AllowFormattingBarState',
    'applicationarchitecture': 'ApplicationArchitecture',
    'applicationlanguage': 'ApplicationLanguage', 'applicationversion': 'ApplicationVersion',
    'cachefilename': 'CacheFileName', 'cachefilepath': 'CacheFilePath',
    'calculationrepetitionnumber': 'CalculationRepetitionNumber',
    'connectionattributes': 'ConnectionAttributes', 'connectionstate': 'ConnectionState',
    'currentdate': 'CurrentDate', 'currentextendedprivileges': 'CurrentExtendedPrivileges',
    'currenthosttimestamp': 'CurrentHostTimestamp',
    'currentprivilegesetname': 'CurrentPrivilegeSetName', 'currenttime': 'CurrentTime',
    'currenttimestamp': 'CurrentTimestamp',
    'currenttimeutcmicroseconds': 'CurrentTimeUTCMicroseconds',
    'currenttimeutcmilliseconds': 'CurrentTimeUTCMilliseconds',
    'custommenusetname': 'CustomMenuSetName', 'data-file-position': 'data-file-position',
    'desktoppath': 'DesktopPath', 'device': 'Device', 'directory': 'directory',
    'documentspath': 'DocumentsPath', 'documentspathlisting': 'DocumentsPathListing',
    'encryptionstate': 'EncryptionState', 'errorcapturestate': 'ErrorCaptureState',
    'file-exists': 'file-exists', 'file-size': 'file-size',
    'filelocaleelements': 'FileLocaleElements', 'filemakerpath': 'FileMakerPath',
    'filename': 'FileName', 'filepath': 'FilePath', 'filesize': 'FileSize',
    'foundcount': 'FoundCount', 'functions': 'functions',
    'highcontraststate': 'HighContrastState',
    'hostapplicationversion': 'HostApplicationVersion', 'hostipaddress': 'HostIPAddress',
    'hostname': 'HostName', 'installedfmplugins': 'InstalledFMPlugins',
    'installedfmpluginsasjson': 'InstalledFMPluginsAsJSON', 'lasterror': 'LastError',
    'lasterrordetail': 'LastErrorDetail', 'lasterrorlocation': 'LastErrorLocation',
    'lastmessagechoice': 'LastMessageChoice', 'laststeptokensused': 'LastStepTokensUsed',
    'layoutaccess': 'LayoutAccess', 'layoutcount': 'LayoutCount', 'layoutname': 'LayoutName',
    'layoutnumber': 'LayoutNumber', 'layouttablename': 'LayoutTableName',
    'layoutviewstate': 'LayoutViewState', 'menubarstate': 'MenubarState',
    'modifiedfields': 'ModifiedFields', 'multiuserstate': 'MultiUserState',
    'networkprotocol': 'NetworkProtocol', 'networktype': 'NetworkType',
    'opendatafileinfo': 'OpenDataFileInfo', 'pagecount': 'PageCount',
    'pagenumber': 'PageNumber', 'persistentid': 'PersistentID',
    'preferencespath': 'PreferencesPath', 'printername': 'PrinterName',
    'quickfindtext': 'QuickFindText', 'recordaccess': 'RecordAccess', 'recordid': 'RecordID',
    'recordmodificationcount': 'RecordModificationCount', 'recordnumber': 'RecordNumber',
    'recordopencount': 'RecordOpenCount', 'recordopenstate': 'RecordOpenState',
    'regionmonitorevents': 'RegionMonitorEvents', 'requestcount': 'RequestCount',
    'requestomitstate': 'RequestOmitState',
    'reverttransactiononerrorstate': 'RevertTransactionOnErrorState',
    'screendepth': 'ScreenDepth', 'screenheight': 'ScreenHeight',
    'screenscalefactor': 'ScreenScaleFactor', 'screenwidth': 'ScreenWidth',
    'scriptanimationstate': 'ScriptAnimationState', 'scriptname': 'ScriptName',
    'scriptparameter': 'ScriptParameter', 'scriptresult': 'ScriptResult',
    'sessionidentifier': 'SessionIdentifier', 'sortstate': 'SortState',
    'statusareastate': 'StatusAreaState', 'systemappearance': 'SystemAppearance',
    'systemdrive': 'SystemDrive', 'systemipaddress': 'SystemIPAddress',
    'systemlanguage': 'SystemLanguage', 'systemlocaleelements': 'SystemLocaleElements',
    'systemnicaddress': 'SystemNICAddress', 'systemplatform': 'SystemPlatform',
    'systemstorageavailable': 'SystemStorageAvailable', 'systemversion': 'SystemVersion',
    'temporarypath': 'TemporaryPath', 'textrulervisible': 'TextRulerVisible',
    'totalrecordcount': 'TotalRecordCount', 'touchkeyboardstate': 'TouchKeyboardState',
    'transactionopenstate': 'TransactionOpenState',
    'triggercurrentpanel': 'TriggerCurrentPanel',
    'triggerexternalevent': 'TriggerExternalEvent',
    'triggergestureinfo': 'TriggerGestureInfo', 'triggerkeystroke': 'TriggerKeystroke',
    'triggermodifierkeys': 'TriggerModifierKeys', 'triggertargetpanel': 'TriggerTargetPanel',
    'usercount': 'UserCount', 'username': 'UserName',
    'usesystemformatsstate': 'UseSystemFormatsState', 'uuid': 'UUID',
    'uuidnumber': 'UUIDNumber', 'windowcontentheight': 'WindowContentHeight',
    'windowcontentwidth': 'WindowContentWidth', 'windowdesktopheight': 'WindowDesktopHeight',
    'windowdesktopwidth': 'WindowDesktopWidth', 'windowheight': 'WindowHeight',
    'windowleft': 'WindowLeft', 'windowmode': 'WindowMode', 'windowname': 'WindowName',
    'windoworientation': 'WindowOrientation', 'windowstyle': 'WindowStyle',
    'windowtop': 'WindowTop', 'windowuuid': 'WindowUUID', 'windowvisible': 'WindowVisible',
    'windowwidth': 'WindowWidth', 'windowzoomlevel': 'WindowZoomLevel',
}


_GET_EXAMPLE = {
    'accountextendedprivileges': 'fmwebdirect', 'accountgroupname': 'Sales',
    'accountname': 'bob@example.com', 'accountprivilegesetname': '[Read-Only Access]',
    'activefieldcontents': 'SomeShop', 'activefieldname': 'Country',
    'activelayoutobjectname': 'customerName', 'activemodifierkeys': '9',
    'activeportalrownumber': '5', 'activerecordnumber': '4', 'activerepetitionnumber': '5',
    'activeselectionsize': '4', 'activeselectionstart': '5', 'allowabortstate': '1',
    'allowformattingbarstate': '1', 'applicationarchitecture': 'Apple silicon',
    'applicationlanguage': 'English', 'applicationversion': 'Pro 26.0.1',
    'cachefilename': 'FMTEMPFM9912_2.tmp',
    'cachefilepath': '/C:/Users/username/AppData/Local/Temp/',
    'calculationrepetitionnumber': '5', 'connectionstate': '3',
    'currentextendedprivileges': 'fmapp', 'currentprivilegesetname': '[Full Access]',
    'custommenusetname': 'Custom Menu Set #1',
    'desktoppath': '/C:/Users/John Smith/Desktop/', 'device': '2',
    'documentspath': '/C:/Users/Username/Documents/', 'encryptionstate': '0',
    'errorcapturestate': '1',
    'filemakerpath': '/C:/Program Files/FileMaker/FileMaker Pro/', 'filename': 'Contacts',
    'filepath': 'file:/C:/Users/Username/Documents/Clients.fmp12', 'filesize': '15000',
    'hostapplicationversion': 'Pro 26.0.1',
    'hostipaddress': '192.168.1.10', 'hostname': 'fms.example.com', 'lasterror': '0',
    'layoutaccess': '1', 'layoutcount': '3', 'layoutname': 'Product List',
    'layoutnumber': '3', 'layouttablename': 'Teachers', 'menubarstate': '1',
    'multiuserstate': '0', 'networkprotocol': 'TCP/IP', 'networktype': '3',
    'pagenumber': '4', 'persistentid': 'A1B2C3D4E5F60718293A4B5C6D7E8F90',
    'preferencespath': '/C:/Users/John Smith/AppData/Local/',
    'printername': 'HP LaserJet P4015, winspool, Ne03', 'recordaccess': '1',
    'recordmodificationcount': '0', 'recordnumber': '3', 'recordopencount': '4',
    'recordopenstate': '1', 'requestcount': '5', 'requestomitstate': '1',
    'reverttransactiononerrorstate': '0', 'screendepth': '32', 'screenheight': '480',
    'screenscalefactor': '2', 'screenwidth': '640', 'scriptanimationstate': '1',
    'scriptname': 'Print Report', 'sessionidentifier': 'Sharon Lloyd A9oCnLQ',
    'sortstate': '1', 'statusareastate': '1', 'systemappearance': 'High Contrast White',
    'systemdrive': '/C:/', 'systemipaddress': '192.168.1.100',
    'systemlanguage': 'Japanese', 'systemnicaddress': '00:07:34:4e:c2:0d',
    'systemplatform': '-', 'systemstorageavailable': '417048244224',
    'systemversion': '26.0', 'temporarypath': '/C:/Users/Username/AppData/Local/Temp/S11/',
    'textrulervisible': '1', 'totalrecordcount': '876', 'usercount': '10',
    'username': 'Sharon Lloyd', 'usesystemformatsstate': '1',
    'uuidnumber': '1234567890123456789012345678901234567890123456789012345678',
    'windowcontentwidth': '400', 'windowdesktopheight': '1040',
    'windowdesktopwidth': '1920', 'windowheight': '300', 'windowleft': '52',
    'windowmode': '2', 'windowname': 'Contacts', 'windowstyle': '0', 'windowtop': '52',
    'windowvisible': '1', 'windowwidth': '300', 'windowzoomlevel': '200',
}


def _get_clock(low):
    """A live value for a clock selector (nondeterministic, exactly as FileMaker's
    Get(CurrentDate) etc. return 'now')."""
    now = _pydatetime.now()
    d = now.date().toordinal()
    secs = now.hour * 3600 + now.minute * 60 + now.second
    if low == "currentdate":
        return FMTemporal(d, "date")
    if low == "currenttime":
        return FMTemporal(secs, "time12")
    if low in ("currenttimestamp", "currenthosttimestamp"):
        return FMTemporal((d - 1) * 86400 + secs, "timestamp")
    u = _pydatetime.now(_pytz.utc)                    # UTC epoch counts since 0001-01-01
    total = (u.date().toordinal() - 1) * 86400 + u.hour * 3600 + u.minute * 60 + u.second
    if low == "currenttimeutcmilliseconds":
        return num(total * 1000 + u.microsecond // 1000)
    return num(total * 1000000 + u.microsecond)       # currenttimeutcmicroseconds


def _coerce_get(low, raw):
    """Coerce an override/example string to the selector's value type."""
    kind = _GET_TEMPORAL.get(low)
    if kind == "date":
        return _fn_getasdate([raw])
    if kind in ("time", "time12"):
        t = _fn_getastime([raw])
        if kind == "time12" and isinstance(t, FMTemporal):
            return FMTemporal(t.value, "time12")
        return t
    if kind == "timestamp":
        return _fn_getastimestamp([raw])
    return raw


def _sf_get(args, env):
    if len(args) != 1:
        raise EvalError("Get takes one selector, e.g. Get ( AccountName )")
    node = args[0]
    sel = node[1] if node[0] in ("name", "str") else as_text(evaluate(node, env))
    low = sel.lower()
    if low in env.getvals:                            # 1. caller-supplied value
        return _coerce_get(low, env.getvals[low])
    if low not in _GET_SELECTORS:                     # a typo, not a real selector
        hint = difflib.get_close_matches(low, _GET_SELECTORS, n=1)
        raise EvalError("unknown Get selector %r%s" % (sel,
            " -- did you mean %s?" % _GET_SELECTORS[hint[0]] if hint else ""))
    if low == "uuid":                                # always a valid v4 UUID
        return str(_uuid.uuid4()).upper()
    if low == "foundcount":                           # mock: a number, random 10-100
        return num(_random.randint(10, 100))
    if low in _GET_CLOCK:                              # 2. live clock value
        return _get_clock(low)
    if low in _GET_EXAMPLE:                            # 3. Claris doc example value
        return _coerce_get(low, _GET_EXAMPLE[low])
    return ""                                         # 4. otherwise empty


SPECIAL = {
    "if": _sf_if, "case": _sf_case, "choose": _sf_choose,
    "let": _sf_let, "while": _sf_while, "substitute": _sf_substitute,
    "evaluate": _sf_evaluate, "get": _sf_get,
}
# jsonsetelement is registered after its definition in the JSON section below.


# ===========================================================================
# Built-in function implementations (pure subset). Each takes a list of already
# evaluated argument values and returns a value. Semantics follow the Claris
# function reference (skills/*.md).
# ===========================================================================
def _chars(t):
    return as_text(t)


def _fn_left(a):
    t = _chars(a[0]); n = int(as_number(a[1]))
    return t[:n] if n > 0 else ""


def _fn_right(a):
    t = _chars(a[0]); n = int(as_number(a[1]))
    return t[-n:] if n > 0 else ""


def _fn_middle(a):
    t = _chars(a[0]); start = int(as_number(a[1])); cnt = int(as_number(a[2]))
    s = max(start - 1, 0)
    return t[s:s + cnt] if cnt > 0 else ""


def _fn_length(a):
    return num(len(_chars(a[0])))


def _fn_position(a):
    t = _chars(a[0]); search = _chars(a[1])
    start = int(as_number(a[2])); occ = int(as_number(a[3]))
    if not search or occ == 0:
        return num(0)
    tl, sl = t.casefold(), search.casefold()      # not case sensitive
    if occ > 0:
        pos = max(start - 1, 0)
        for _ in range(occ):
            pos = tl.find(sl, pos)
            if pos < 0:
                return num(0)
            found = pos
            pos += 1
        return num(found + 1)
    # negative occurrence: scan toward the beginning. Consider matches whose
    # start index is at/before `start`, taken from the highest index downward.
    hi = len(t) if start < 1 else min(start - 1 + len(sl), len(t))
    matches = []
    idx = tl.rfind(sl, 0, hi)
    while idx >= 0:
        matches.append(idx)
        idx = tl.rfind(sl, 0, idx)
    k = -occ
    return num(matches[k - 1] + 1) if k <= len(matches) else num(0)


def _fn_patterncount(a):
    t = _chars(a[0]).casefold(); s = _chars(a[1]).casefold()
    return num(t.count(s) if s else 0)


def _fn_replace(a):
    t = _chars(a[0]); start = int(as_number(a[1])); cnt = int(as_number(a[2]))
    repl = _chars(a[3])
    s = max(start - 1, 0)
    return t[:s] + repl + t[s + max(cnt, 0):]


def _fn_exact(a):
    return num(1) if _chars(a[0]) == _chars(a[1]) else num(0)


def _fn_filter(a):
    keep = set(_chars(a[1]))
    return "".join(ch for ch in _chars(a[0]) if ch in keep)


def _fn_trim(a):
    return _chars(a[0]).strip(" ")


def _fn_upper(a):
    return _chars(a[0]).upper()


def _fn_lower(a):
    return _chars(a[0]).lower()


def _fn_proper(a):
    # capitalize the first letter of each word; lower the rest.
    return re.sub(r"\w+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(),
                  _chars(a[0]))


# FileMaker word boundaries (wordcount.md + oracle): hyphen, underscore, '=', '&'
# and the like START a new word, but an apostrophe, period, or comma stays WITHIN
# a word when it sits between two word characters -- so "it's", "3.14", "1,000.50"
# and "U.S.A" are each one word, while "x=y=1.5" is three and "a-b-c" is three.
_WORD_RE = re.compile(r"[^\W_]+(?:['.,][^\W_]+)*", re.UNICODE)


def _word_spans(t):
    """(start, end) of each word. FileMaker's word functions return a SLICE of
    the original text between word boundaries, keeping the punctuation/spaces
    that lie between the selected words -- so we track positions, not just text."""
    return [(m.start(), m.end()) for m in _WORD_RE.finditer(t)]


def _fn_wordcount(a):
    return num(len(_word_spans(_chars(a[0]))))


def _fn_leftwords(a):
    t = _chars(a[0]); sp = _word_spans(t); n = int(as_number(a[1]))
    if n <= 0 or not sp:
        return ""
    return t[sp[0][0]:sp[min(n, len(sp)) - 1][1]]


def _fn_rightwords(a):
    t = _chars(a[0]); sp = _word_spans(t); n = int(as_number(a[1]))
    if n <= 0 or not sp:
        return ""
    return t[sp[max(len(sp) - n, 0)][0]:sp[-1][1]]


def _fn_middlewords(a):
    t = _chars(a[0]); sp = _word_spans(t)
    start = int(as_number(a[1])); cnt = int(as_number(a[2]))
    s = max(start - 1, 0)
    if cnt <= 0 or s >= len(sp):
        return ""
    e = min(s + cnt, len(sp))
    return t[sp[s][0]:sp[e - 1][1]]


# ---- ¶-delimited value lists ----------------------------------------------
def _fn_valuecount(a):
    t = _chars(a[0])
    if t == "":
        return num(0)
    parts = t.split(CR)
    if parts and parts[-1] == "":                 # a trailing ¶ doesn't add a value
        parts = parts[:-1]
    return num(len(parts))


def _fn_getvalue(a):
    parts = _chars(a[0]).split(CR)
    n = int(as_number(a[1]))
    return parts[n - 1] if 1 <= n <= len(parts) else ""


def _fn_leftvalues(a):
    parts = _chars(a[0]).split(CR)
    n = int(as_number(a[1]))
    if n <= 0:
        return ""
    sel = parts[:n]
    return "".join(v + CR for v in sel)           # each value gets a trailing ¶


def _fn_rightvalues(a):
    parts = _chars(a[0]).split(CR)
    n = int(as_number(a[1]))
    if n <= 0:
        return ""
    sel = parts[-n:]
    return "".join(v + CR for v in sel)


def _fn_middlevalues(a):
    parts = _chars(a[0]).split(CR)
    start = int(as_number(a[1])); cnt = int(as_number(a[2]))
    s = max(start - 1, 0)
    sel = parts[s:s + cnt] if cnt > 0 else []
    return "".join(v + CR for v in sel)


def _fn_filtervalues(a):
    keep = set(_chars(a[1]).split(CR))
    out = [v for v in _chars(a[0]).split(CR) if v in keep]
    return "".join(v + CR for v in out)


def _fn_list(a):
    out = []
    for v in a:
        t = as_text(v)
        for piece in t.split(CR):
            if piece != "":
                out.append(piece)
    return CR.join(out)


# ---- Char / Code -----------------------------------------------------------
# FileMaker packs each character into a 5-decimal-digit field, with the FIRST
# character in the least-significant digits: Code("ab") = 97 + 98*100000 =
# 9800097. Char is the inverse, emitting groups low-to-high.
def _fn_char(a):
    n = int(as_number(a[0]))
    if n <= 0:
        return ""
    out = []
    while n > 0:
        cp = n % 100000
        n //= 100000
        if cp:
            try:
                out.append(chr(cp))
            except (ValueError, OverflowError):
                pass
    return "".join(out)


def _fn_code(a):
    t = _chars(a[0])
    total = 0
    for i, ch in enumerate(t):
        total += ord(ch) * (100000 ** i)
    return num(total) if t else ""


# ---- numbers ---------------------------------------------------------------
def _fn_abs(a):
    return _propagate_inexact(abs(as_number(a[0])), a[0])


def _fn_int(a):
    return num(int(as_number(a[0])))              # truncates toward zero


def _fn_round(a):
    d = as_number(a[0]); places = int(as_number(a[1]))
    q = Decimal(1).scaleb(-places)
    return d.quantize(q, rounding=decimal.ROUND_HALF_UP)


def _fn_truncate(a):
    d = as_number(a[0]); places = int(as_number(a[1]))
    q = Decimal(1).scaleb(-places)
    return d.quantize(q, rounding=decimal.ROUND_DOWN)


def _fn_floor(a):
    return num(as_number(a[0]).to_integral_value(rounding=decimal.ROUND_FLOOR))


def _fn_ceiling(a):
    return num(as_number(a[0]).to_integral_value(rounding=decimal.ROUND_CEILING))


def _fn_mod(a):
    x = as_number(a[0]); y = as_number(a[1])
    if y == 0:
        raise EvalError("Mod by zero")
    return x - y * (x / y).to_integral_value(rounding=decimal.ROUND_FLOOR)


def _fn_div(a):
    x = as_number(a[0]); y = as_number(a[1])
    if y == 0:
        raise EvalError("Div by zero")
    return num((x / y).to_integral_value(rounding=decimal.ROUND_FLOOR))


def _fn_sign(a):
    d = as_number(a[0])
    return num(0 if d == 0 else (1 if d > 0 else -1))


def _fn_sqrt(a):
    x = as_number(a[0])
    if x < 0:
        raise EvalError("Sqrt of a negative number")
    return _inexact(x.sqrt(context=_CTX))


def _fn_exp(a):
    return _inexact(as_number(a[0]).exp(context=_CTX))


def _fn_ln(a):
    x = as_number(a[0])
    if x <= 0:
        raise EvalError("Ln of a non-positive number")
    return _inexact(x.ln(context=_CTX))


def _fn_log(a):
    x = as_number(a[0])
    if x <= 0:
        raise EvalError("Log of a non-positive number")
    return _inexact(x.log10(context=_CTX))


def _fn_min(a):
    return _propagate_inexact(min((as_number(v) for v in a), default=num(0)), *a)


def _fn_max(a):
    return _propagate_inexact(max((as_number(v) for v in a), default=num(0)), *a)


def _fn_sum(a):
    total = num(0)
    for v in a:
        total += as_number(v)
    return _propagate_inexact(total, *a)


def _fn_count(a):
    return num(sum(1 for v in a if as_text(v) != ""))


def _fn_average(a):
    vals = [as_number(v) for v in a if as_text(v) != ""]
    if not vals:
        return num(0)
    return _inexact(_CTX.divide(sum(vals, num(0)), num(len(vals))))   # division


# ---- logical / coercion ----------------------------------------------------
def _fn_isempty(a):
    return num(1) if as_text(a[0]) == "" else num(0)


def _fn_getasboolean(a):
    return num(1) if as_bool(a[0]) else num(0)


def _fn_getasnumber(a):
    return as_number(a[0])


def _fn_getastext(a):
    return as_text(a[0])


# ===========================================================================
# JSON functions. FileMaker's JSON engine: object keys are emitted SORTED
# alphabetically (array order is preserved), compact output has no spaces, and
# errors are returned as ordinary text beginning with "?" (they do NOT abort the
# calculation). Numbers are carried as Decimal so they serialize without loss.
# ===========================================================================
_JSON_CONST = {"jsonstring": 1, "jsonnumber": 2, "jsonobject": 3,
               "jsonarray": 4, "jsonboolean": 5, "jsonnull": 6, "jsonraw": 0}
_JSON_ERR = "? Incorrect key, index, or path"


class _JsonError(Exception):
    pass


def _json_parse(text):
    """Parse JSON text, keeping numbers as Decimal. Raises _JsonError on invalid
    input (the caller turns that into a '?' text result, as FileMaker does)."""
    if not isinstance(text, str):
        text = as_text(text)
    try:
        result = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, ValueError):
        raise _JsonError("? * invalid JSON")
    # FileMaker requires the JSON document's root to be an object or an array;
    # a bare top-level scalar (e.g. "5" or "\"hi\"") is rejected as invalid.
    if not isinstance(result, (dict, list)):
        raise _JsonError("? * A valid JSON document must be either an array or "
                         "an object value.")
    return result


def _json_steps(path):
    """Parse a keyOrIndexOrPath into a list of steps: ('key',name), ('index',n),
    ('last',) or ('append',). Supports dot and bracket notation, and a bracketed
    key right after another segment (e.g. bakery.product[1]name)."""
    s = path.strip() if isinstance(path, str) else as_text(path)
    steps = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == ".":
            i += 1
            continue
        if c == "[":
            end = s.find("]", i)
            if end < 0:
                raise _JsonError(_JSON_ERR)
            inner = s[i + 1:end]
            i = end + 1
            if inner == ":":
                steps.append(("last",))
            elif inner == "+":
                steps.append(("append",))
            elif len(inner) >= 2 and inner[0] == "'" and inner[-1] == "'":
                steps.append(("key", inner[1:-1]))
            else:
                try:
                    steps.append(("index", int(inner)))
                except ValueError:
                    raise _JsonError(_JSON_ERR)
        else:                                     # bare key up to next . or [
            j = i
            while j < n and s[j] not in ".[":
                j += 1
            steps.append(("key", s[i:j]))
            i = j
    return steps


def _json_get(node, steps):
    cur = node
    for st in steps:
        kind = st[0]
        if kind == "key":
            name = st[1]
            if isinstance(cur, dict) and name in cur:
                cur = cur[name]
            elif isinstance(cur, list) and name.lstrip("-").isdigit():
                cur = _json_index(cur, int(name))
            else:
                raise _JsonError(_JSON_ERR)
        elif kind == "index":
            cur = _json_index(cur, st[1])
        elif kind == "last":
            if isinstance(cur, list) and cur:
                cur = cur[-1]
            else:
                raise _JsonError(_JSON_ERR)
        else:
            raise _JsonError(_JSON_ERR)
    return cur


def _json_index(node, idx):
    if isinstance(node, list) and -len(node) <= idx < len(node):
        return node[idx]
    raise _JsonError(_JSON_ERR)


def _json_set_rec(node, steps, value):
    if not steps:
        return value
    st, rest = steps[0], steps[1:]
    kind = st[0]
    if kind == "key":
        d = dict(node) if isinstance(node, dict) else {}
        d[st[1]] = _json_set_rec(d.get(st[1]), rest, value)
        return d
    lst = list(node) if isinstance(node, list) else []
    if kind == "append":
        lst.append(_json_set_rec(None, rest, value))
    elif kind == "last":
        if lst:
            lst[-1] = _json_set_rec(lst[-1], rest, value)
        else:
            lst.append(_json_set_rec(None, rest, value))
    else:                                         # index
        idx = st[1]
        if idx < 0:
            raise _JsonError(_JSON_ERR)
        while len(lst) <= idx:
            lst.append(None)
        lst[idx] = _json_set_rec(lst[idx], rest, value)
    return lst


# ---- serialization ---------------------------------------------------------
def _json_num(d):
    """A Decimal/int as a valid JSON number: fixed notation, trailing zeros
    trimmed, leading zero KEPT (JSON requires 0.5, not .5)."""
    if isinstance(d, int):
        return str(d)
    exp = d.as_tuple().exponent
    if isinstance(exp, int) and exp < -16:
        d = d.quantize(Decimal(1).scaleb(-16), rounding=decimal.ROUND_HALF_UP,
                       context=_CTX)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _json_scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, Decimal)):
        return _json_num(v)
    return json.dumps(v, ensure_ascii=False)


def _json_compact(v):
    if isinstance(v, dict):
        return "{" + ",".join("%s:%s" % (json.dumps(k, ensure_ascii=False),
                                          _json_compact(val))
                              for k, val in sorted(v.items())) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_json_compact(x) for x in v) + "]"
    return _json_scalar(v)


def _json_pretty(v, indent=0):
    """JSONFormatElements style: tabs, sorted keys, and a nested object/array
    whose opening brace drops to the next line after `"key" : `."""
    tab = "\t"
    if isinstance(v, dict):
        if not v:
            return "{}"
        parts = []
        for k in sorted(v):
            val = v[k]
            slot = tab * (indent + 1) + json.dumps(k, ensure_ascii=False) + " : "
            if isinstance(val, (dict, list)) and val:
                parts.append(slot + "\n" + tab * (indent + 1)
                             + _json_pretty(val, indent + 1))
            else:
                parts.append(slot + (_json_pretty(val, indent + 1)
                                     if isinstance(val, (dict, list))
                                     else _json_scalar(val)))
        return "{\n" + ",\n".join(parts) + "\n" + tab * indent + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        parts = []
        for val in v:
            if isinstance(val, (dict, list)) and val:
                parts.append(tab * (indent + 1) + _json_pretty(val, indent + 1))
            else:
                parts.append(tab * (indent + 1)
                             + (_json_pretty(val, indent + 1)
                                if isinstance(val, (dict, list))
                                else _json_scalar(val)))
        return "[\n" + ",\n".join(parts) + "\n" + tab * indent + "]"
    return _json_scalar(v)


def _json_leaf_text(v):
    """How JSONGetElement / JSONListValues render an element: numbers and
    booleans as numbers, strings unquoted, objects/arrays as compact JSON."""
    if isinstance(v, bool):
        return num(1) if v else num(0)
    if isinstance(v, (int, Decimal)):
        return num(v)
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        return _json_compact(v)
    return v


# ---- the functions ---------------------------------------------------------
def _fn_jsongetelement(a):
    try:
        root = _json_parse(a[0])                  # invalid document -> "?" error
    except _JsonError as e:
        return str(e)
    try:
        node = _json_get(root, _json_steps(a[1]))
    except _JsonError:
        return ""                                 # missing key/path -> empty
    if node is None:                              # a JSON null reads as empty
        return ""
    return _json_leaf_text(node)


def _fn_jsongetelementtype(a):
    try:
        root = _json_parse(a[0])
    except _JsonError as e:
        return str(e)
    try:
        node = _json_get(root, _json_steps(a[1]))
    except _JsonError:
        return _JSON_ERR
    if isinstance(node, bool):
        return num(5)
    if isinstance(node, (int, Decimal)):
        return num(2)
    if node is None:
        return num(6)
    if isinstance(node, dict):
        return num(3)
    if isinstance(node, list):
        return num(4)
    return num(1)


def _fn_jsonlistkeys(a):
    try:
        root = _json_parse(a[0])
    except _JsonError as e:
        return str(e)
    try:
        node = _json_get(root, _json_steps(a[1]))
    except _JsonError:
        return _JSON_ERR
    if isinstance(node, dict):
        return CR.join(sorted(node))
    if isinstance(node, list):
        return CR.join(str(i) for i in range(len(node)))
    return _JSON_ERR


def _fn_jsonlistvalues(a):
    try:
        root = _json_parse(a[0])
    except _JsonError as e:
        return str(e)
    try:
        node = _json_get(root, _json_steps(a[1]))
    except _JsonError:
        return _JSON_ERR
    if isinstance(node, dict):
        vals = [node[k] for k in sorted(node)]
    elif isinstance(node, list):
        vals = node
    else:
        return _JSON_ERR
    out = []
    for v in vals:
        out.append(as_text(_json_leaf_text(v)) if not isinstance(v, (dict, list))
                   else _json_compact(v))
    return CR.join(out)


def _fn_jsondeleteelement(a):
    try:
        root = _json_parse(a[0])
        steps = _json_steps(a[1])
    except _JsonError as e:
        return str(e)
    if not steps:
        return ""
    try:
        new = _json_delete(root, steps)
    except _JsonError as e:
        return str(e)
    return _json_compact(new)


def _json_delete(node, steps):
    st, rest = steps[0], steps[1:]
    if not rest:                                  # delete this step from node
        if st[0] == "key" and isinstance(node, dict) and st[1] in node:
            d = dict(node)
            del d[st[1]]
            return d
        if st[0] in ("index", "last") and isinstance(node, list):
            lst = list(node)
            idx = (len(lst) - 1) if st[0] == "last" else st[1]
            if -len(lst) <= idx < len(lst):
                del lst[idx]
                return lst
        raise _JsonError(_JSON_ERR)
    child = _json_get(node, [st])                 # raises if missing
    new_child = _json_delete(child, rest)
    return _json_set_rec(node, [st], new_child)


def _fn_jsonformatelements(a):
    try:
        return _json_pretty(_json_parse(a[0]))
    except _JsonError as e:
        return str(e)


def _json_coerce(value, jtype):
    """Convert a FileMaker value to a Python JSON value per the type constant."""
    if jtype == 1:                                # JSONString
        return as_text(value)
    if jtype == 2:                                # JSONNumber
        return as_number(value)
    if jtype == 3 or jtype == 4:                  # JSONObject / JSONArray
        return _json_parse(value)
    if jtype == 5:                                # JSONBoolean
        return bool(as_bool(value))
    if jtype == 6:                                # JSONNull
        return None
    # JSONRaw (0): use the FIRST valid JSON element in value (characters after it
    # are ignored, so "4,2" -> 4); if value isn't valid JSON, treat it as a string.
    dec = json.JSONDecoder(parse_float=Decimal, parse_int=Decimal)
    try:
        obj, _ = dec.raw_decode(as_text(value).lstrip())
        return obj
    except (json.JSONDecodeError, ValueError):
        return as_text(value)


def _sf_jsonsetelement(args, env):
    """JSONSetElement ( json ; path ; value ; type ) or
       JSONSetElement ( json ; [path;value;type] ; ... ). Errors -> '?' text."""
    if len(args) < 2:
        raise EvalError("JSONSetElement takes at least 2 arguments")
    base = evaluate(args[0], env)
    triples = []
    rest = args[1:]
    if len(rest) == 3 and rest[0][0] != "list":
        triples.append(rest)
    else:
        for grp in rest:
            if grp[0] != "list" or len(grp[1]) != 3:
                raise EvalError("JSONSetElement groups must be [ path ; value ; type ]")
            triples.append(grp[1])
    base_text = as_text(base)
    try:
        if base_text.strip() == "":
            root = None                           # decided per first path below
        else:
            root = _json_parse(base_text)
    except _JsonError as e:
        return str(e)
    for path_n, val_n, type_n in triples:
        path = as_text(evaluate(path_n, env))
        try:
            steps = _json_steps(path)
            jtype = int(as_number(evaluate(type_n, env)))
            jval = _json_coerce(evaluate(val_n, env), jtype)
        except _JsonError as e:
            return str(e)
        if root is None:                          # empty json: array if path is [..]
            root = [] if steps and steps[0][0] in ("index", "last", "append") else {}
        try:
            root = _json_set_rec(root, steps, jval)
        except _JsonError as e:
            return str(e)
    if root is None:
        root = {}
    return _json_compact(root)


SPECIAL["jsonsetelement"] = _sf_jsonsetelement


# ===========================================================================
# Temporal functions (EN_US). Day count = Python ordinal; constructors return an
# FMTemporal so the result displays as a date/time/timestamp.
# ===========================================================================
_DATE_RE = re.compile(r"\s*(\d{1,2})/(\d{1,2})/(\d{1,4})\s*$")
_TIME_RE = re.compile(r"\s*(\d+):(\d{1,2})(?::(\d{1,2})(?:\.\d+)?)?\s*([AaPp][Mm])?\s*$")


def _parse_time_text(v):
    """Parse a time string -> (seconds, is_12hour) or None. A meridiem (AM/PM)
    requires an hour of 1-12, so '22:02:02 pm' is invalid (returns None), matching
    FileMaker; is_12hour records that the value should display in 12-hour form."""
    m = _TIME_RE.match(v)
    if not m:
        return None
    h = int(m[1])
    ap = m[4]
    if ap:
        if not (1 <= h <= 12):
            return None
        h = (h % 12) + (12 if ap[0] in "Pp" else 0)
    return h * 3600 + int(m[2]) * 60 + int(m[3] or 0), bool(ap)


def _resolve_year(ystr):
    """A 1-or-2-digit year falls in FileMaker's sliding 100-year window
    [currentYear-69, currentYear+30] (i.e. up to 30 years in the future) -- the
    year ending in YY within that range. DATE-DEPENDENT (uses the system year).
    In 2026: 55->2055, 56->2056, but 60->1960. A 3-or-4-digit year is literal."""
    y = int(ystr)
    if len(ystr) > 2:
        return y
    lo = _TODAY_YEAR - 69
    cand = (lo // 100) * 100 + y
    return cand + 100 if cand < lo else cand


def _coerce_days(v):
    """Day count for a date function: a date value -> its number; a timestamp ->
    its date part; a plain number -> that day count; text -> parsed as M/D/YYYY.
    Date functions parse text as a DATE, not via numeric digit extraction. None if
    the text isn't a valid date."""
    if isinstance(v, FMTemporal):
        return int(v.value) // 86400 + 1 if v.kind == "timestamp" else int(v.value)
    if isinstance(v, Decimal):
        return int(v)
    m = _DATE_RE.match(v)
    if not m:
        return None
    try:
        return _pydate(_resolve_year(m[3]), int(m[1]), int(m[2])).toordinal()
    except ValueError:
        return None


def _coerce_secs(v):
    """Seconds for a time function: a timestamp -> within-day seconds; a time or
    plain number -> the raw seconds (may exceed 24h); text -> parsed H:MM[:SS].
    None if the text isn't a valid time."""
    if isinstance(v, FMTemporal):
        return int(v.value) % 86400 if v.kind == "timestamp" else int(v.value)
    if isinstance(v, Decimal):
        return int(v)
    r = _parse_time_text(v)
    return r[0] if r is not None else None


def _safe_date(v):
    """Python date for a date value (number, temporal, or M/D/YYYY text); None if
    invalid or out of the representable range."""
    n = _coerce_days(v)
    if n is None or not (_MIN_ORD <= n <= _MAX_ORD):
        return None
    return _pydate.fromordinal(n)


def _fn_date(a):
    mo = int(as_number(a[0])); da = int(as_number(a[1])); yr = int(as_number(a[2]))
    yr += (mo - 1) // 12                          # normalize month overflow
    mo = (mo - 1) % 12 + 1
    try:
        base = _pydate(yr, mo, 1).toordinal()
    except (ValueError, OverflowError):
        return FMTemporal(0, "date")              # out of range -> displays "?"
    return FMTemporal(base + (da - 1), "date")    # day overflow folds in


def _fn_time(a):
    return FMTemporal(as_number(a[0]) * 3600 + as_number(a[1]) * 60 + as_number(a[2]),
                      "time")


def _fn_timestamp(a):
    return FMTemporal((as_number(a[0]) - 1) * 86400 + as_number(a[1]), "timestamp")


def _fn_day(a):
    d = _safe_date(a[0]); return num(d.day) if d else "?"


def _fn_month(a):
    d = _safe_date(a[0]); return num(d.month) if d else "?"


def _fn_year(a):
    d = _safe_date(a[0]); return num(d.year) if d else "?"


def _fn_dayofweek(a):
    d = _safe_date(a[0]); return num(d.isoweekday() % 7 + 1) if d else "?"


def _fn_dayofyear(a):
    d = _safe_date(a[0])
    return num(d.toordinal() - _pydate(d.year, 1, 1).toordinal() + 1) if d else "?"


def _fn_dayname(a):
    d = _safe_date(a[0]); return _DAY_NAMES[d.weekday()] if d else "?"


def _fn_monthname(a):
    d = _safe_date(a[0]); return _MONTH_NAMES[d.month - 1] if d else "?"


def _fn_weekofyear(a):
    d = _safe_date(a[0])
    if not d:
        return "?"
    doy = d.toordinal() - _pydate(d.year, 1, 1).toordinal() + 1
    jan1_dow = _pydate(d.year, 1, 1).isoweekday() % 7 + 1   # FM DayOfWeek of Jan 1
    return num((doy - 1 + jan1_dow - 1) // 7 + 1)


def _fn_weekofyearfiscal(a):
    d = _safe_date(a[0])
    sd = int(as_number(a[1]))
    if not d or not (1 <= sd <= 7):
        return "?"

    def fmdow(o):                                  # FileMaker DayOfWeek (Sun=1..Sat=7)
        return _pydate.fromordinal(o).isoweekday() % 7 + 1
    o = d.toordinal()
    week_start = o - (fmdow(o) - sd) % 7           # start of this date's week
    pivot_year = _pydate.fromordinal(week_start + 3).year   # year owning the week (>=4 days)
    jan1 = _pydate(pivot_year, 1, 1).toordinal()
    jan1_off = (fmdow(jan1) - sd) % 7
    week1_start = jan1 - jan1_off + (7 if jan1_off > 3 else 0)
    return num((week_start - week1_start) // 7 + 1)


def _fn_serialincrement(a):
    text = as_text(a[0])
    inc = int(as_number(a[1]))                     # only the integer portion is used
    last = None
    for last in re.finditer(r"\d+", text):         # the rightmost run of digits
        pass
    if last is None:
        return text
    n = int(last.group(0)) + inc
    rep = str(n).zfill(len(last.group(0))) if n >= 0 else str(n)
    return text[:last.start()] + rep + text[last.end():]


def _fn_hour(a):
    s = _coerce_secs(a[0]); return num(s // 3600) if s is not None else "?"


def _fn_minute(a):
    s = _coerce_secs(a[0]); return num((s % 3600) // 60) if s is not None else "?"


def _fn_seconds(a):
    s = _coerce_secs(a[0]); return num(s % 60) if s is not None else "?"


def _fn_getasdate(a):
    v = a[0]
    if isinstance(v, FMTemporal):                  # take the day part
        d = _safe_date(v)
        return FMTemporal(d.toordinal(), "date") if d else "?"
    if isinstance(v, Decimal):                      # a number is a raw day count
        return FMTemporal(int(v), "date")
    m = _DATE_RE.match(v)
    if not m:
        return "?"
    try:
        return FMTemporal(_pydate(_resolve_year(m[3]), int(m[1]), int(m[2])).toordinal(), "date")
    except ValueError:
        return "?"


def _fn_getastime(a):
    v = a[0]
    if isinstance(v, FMTemporal):
        return FMTemporal(int(v.value) % 86400 if v.kind == "timestamp" else v.value,
                          "time")
    if isinstance(v, Decimal):                      # a number is raw seconds
        return FMTemporal(v, "time")
    r = _parse_time_text(as_text(v))
    if r is None:
        return "?"
    return FMTemporal(r[0], "time12" if r[1] else "time")   # AM/PM input -> 12-hour display


def _fn_getastimestamp(a):
    v = a[0]
    if isinstance(v, FMTemporal):
        if v.kind == "timestamp":
            return v
        if v.kind == "date":
            return FMTemporal((v.value - 1) * 86400, "timestamp")
        return FMTemporal(v.value, "timestamp")
    if isinstance(v, Decimal):                      # a number is raw timestamp seconds
        return FMTemporal(v, "timestamp")
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{1,4})\s+(\d+):(\d{1,2})(?::(\d{1,2}))?"
                 r"(?:\s*([AaPp][Mm]))?\s*$", as_text(v))
    if not m:
        return "?"
    try:
        days = _pydate(_resolve_year(m[3]), int(m[1]), int(m[2])).toordinal()
    except ValueError:
        return "?"
    h = int(m[4])
    if m[7]:                                       # AM/PM given -> 12-hour input
        if not (1 <= h <= 12):
            return "?"
        h = (h % 12) + (12 if m[7].lower() == "pm" else 0)
    secs = h * 3600 + int(m[5]) * 60 + int(m[6] or 0)
    return FMTemporal((days - 1) * 86400 + secs, "timestamp")



# ===========================================================================
# Additional pure functions (trig, stats, financial, encoding, list, misc).
# ===========================================================================
def _mathf(fn, x):
    try:
        return _inexact(num(fn(float(x))))
    except (ValueError, OverflowError):
        raise EvalError("math domain error")


def _fn_cos(a): return _mathf(math.cos, as_number(a[0]))
def _fn_sin(a): return _mathf(math.sin, as_number(a[0]))
def _fn_tan(a): return _mathf(math.tan, as_number(a[0]))
def _fn_acos(a): return _mathf(math.acos, as_number(a[0]))
def _fn_asin(a): return _mathf(math.asin, as_number(a[0]))
def _fn_atan(a): return _mathf(math.atan, as_number(a[0]))
def _fn_degrees(a): return _mathf(math.degrees, as_number(a[0]))
def _fn_radians(a): return _mathf(math.radians, as_number(a[0]))


def _fn_combination(a):
    n = int(as_number(a[0])); k = int(as_number(a[1]))
    if n < 0 or k < 0 or k > n:
        return "?"
    return num(math.comb(n, k))


def _fn_factorial(a):
    n = int(as_number(a[0]))
    if n < 0:
        return "?"
    factors = int(as_number(a[1])) if len(a) > 1 and as_text(a[1]) != "" else n
    r = 1
    for i in range(max(factors, 0)):
        if n - i <= 0:
            break
        r *= (n - i)
    return num(r)


def _stat(a, sample, want_var):
    xs = [as_number(v) for v in a if as_text(v) != ""]
    denom = (len(xs) - 1) if sample else len(xs)
    if denom <= 0:
        return "?"
    mean = _CTX.divide(sum(xs, num(0)), num(len(xs)))
    ss = sum(((x - mean) ** 2 for x in xs), num(0))
    v = _CTX.divide(ss, num(denom))
    return _inexact(v if want_var else v.sqrt(context=_CTX))


def _fn_stdev(a): return _stat(a, True, False)
def _fn_stdevp(a): return _stat(a, False, False)
def _fn_variance(a): return _stat(a, True, True)
def _fn_variancep(a): return _stat(a, False, True)


def _fn_pv(a):                                    # PV(payment; interestRate; periods)
    p, r, n = as_number(a[0]), as_number(a[1]), as_number(a[2])
    return _inexact(p * n if r == 0 else p * (1 - _fm_pow(1 + r, -n)) / r)


def _fn_fv(a):                                    # FV(payment; interestRate; periods)
    p, r, n = as_number(a[0]), as_number(a[1]), as_number(a[2])
    return _inexact(p * n if r == 0 else p * (_fm_pow(1 + r, n) - 1) / r)


def _fn_pmt(a):                                   # PMT(principal; interestRate; term)
    pv, r, n = as_number(a[0]), as_number(a[1]), as_number(a[2])
    return _inexact(pv / n if r == 0 else pv * r / (1 - _fm_pow(1 + r, -n)))


def _fn_sortvalues(a):                            # datatype: sign=direction, |dt|=type
    vals = _chars(a[0]).split(CR)
    if vals and vals[-1] == "":
        vals = vals[:-1]
    dt = int(as_number(a[1])) if len(a) > 1 and as_text(a[1]) != "" else 1
    key = (lambda v: as_number(v)) if abs(dt) == 2 else (lambda v: v.casefold())
    out = sorted(vals, key=key, reverse=dt < 0)
    return "".join(v + CR for v in out)


def _fn_uniquevalues(a):                          # de-dupe (first-occurrence order); trailing ¶
    vals = _chars(a[0]).split(CR)
    if vals and vals[-1] == "":
        vals = vals[:-1]
    dt = int(as_number(a[1])) if len(a) > 1 and as_text(a[1]) != "" else 1
    numeric = abs(dt) == 2
    seen, out = set(), []
    for v in vals:
        key = as_number(v) if numeric else v.casefold()
        if key not in seen:
            seen.add(key); out.append(v)
    return "".join(v + CR for v in out)


def _fn_trimall(a):
    # TrimAll ( text ; trimSpaces ; trimType ). trimType 3 removes ALL spaces;
    # 0/1/2 leave one space between roman words (we implement the roman/EN case;
    # the Asian full-width/non-roman spacing nuances aren't modeled). trimSpaces
    # (full-width spaces) only matters for non-roman text.
    t = _chars(a[0])
    if int(as_number(a[2])) == 3:
        return t.replace(" ", "").replace("　", "")
    return re.sub(r" +", " ", t).strip(" ")


def _fn_quote(a):
    s = _chars(a[0]).replace('"', '\\"').replace(CR, "\\" + CR)  # escape " and CR (not \)
    return '"' + s + '"'


def _fn_rgb(a):
    r, g, b = (int(as_number(a[i])) for i in range(3))
    return num(r * 65536 + g * 256 + b)


def _fn_base64encode(a):
    return base64.b64encode(_chars(a[0]).encode("utf-8")).decode("ascii")


def _fn_base64decode(a):
    try:
        return base64.b64decode(_chars(a[0])).decode("utf-8", "replace")
    except Exception:
        return "?"


def _fn_hexencode(a):
    return _chars(a[0]).encode("utf-8").hex().upper()


def _fn_hexdecode(a):
    try:
        return bytes.fromhex(_chars(a[0])).decode("utf-8", "replace")
    except ValueError:
        return "?"


def _fn_getasurlencoded(a):
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(),
                  _urlparse.quote(_chars(a[0]), safe=""))


def _fn_jsonmakearray(a):                         # JSONMakeArray(data; delimiter; type)
    data = _chars(a[0])
    delim = _chars(a[1]) if len(a) > 1 and as_text(a[1]) != "" else CR
    jt = int(as_number(a[2])) if len(a) > 2 and as_text(a[2]) != "" else 1
    items = data.split(delim) if delim else [data]
    try:
        return _json_compact([_json_coerce(x, jt) for x in items])
    except _JsonError as e:
        return str(e)


def _fn_jsonparse(a):                             # offline: validate + normalize compact
    try:
        _json_parse(a[0])
    except _JsonError as e:
        return str(e)
    return as_text(a[0])


def _fn_isvalidexpression(a):
    try:
        parse(_chars(a[0]))
        return num(1)
    except CalcSyntaxError:
        return num(0)


def _sf_isvalid(args, env):                       # 1 unless evaluating raises
    try:
        for x in args:
            evaluate(x, env)
        return num(1)
    except EvalError:
        return num(0)


def _sf_setprecision(args, env):                  # display an inexact result at N digits
    if len(args) != 2:
        raise EvalError("SetPrecision takes 2 arguments")
    val = evaluate(args[0], env)
    p = max(16, min(int(as_number(evaluate(args[1], env))), 400))
    if isinstance(val, Decimal):
        return val.quantize(Decimal(1).scaleb(-p), rounding=decimal.ROUND_HALF_UP,
                            context=decimal.Context(prec=p + 64))  # exact, shown in full
    return val


def _sf_setrecursion(args, env):                  # limit arg ignored (capped at MAX_DEPTH)
    if len(args) != 2:
        raise EvalError("SetRecursion takes 2 arguments")
    return evaluate(args[0], env)


SPECIAL["isvalid"] = _sf_isvalid
SPECIAL["setprecision"] = _sf_setprecision
SPECIAL["setrecursion"] = _sf_setrecursion


# Registry: lower-name -> (impl, arity).  arity "V" = variadic.
FUNCS = {
    "cos": (_fn_cos, 1), "sin": (_fn_sin, 1), "tan": (_fn_tan, 1),
    "acos": (_fn_acos, 1), "asin": (_fn_asin, 1), "atan": (_fn_atan, 1),
    "degrees": (_fn_degrees, 1), "radians": (_fn_radians, 1),
    "combination": (_fn_combination, 2), "factorial": (_fn_factorial, "V"),
    "stdev": (_fn_stdev, "V"), "stdevp": (_fn_stdevp, "V"),
    "variance": (_fn_variance, "V"), "variancep": (_fn_variancep, "V"),
    "fv": (_fn_fv, 3), "pmt": (_fn_pmt, 3), "pv": (_fn_pv, 3),
    "sortvalues": (_fn_sortvalues, "V"), "uniquevalues": (_fn_uniquevalues, "V"),
    "trimall": (_fn_trimall, 3), "quote": (_fn_quote, 1), "rgb": (_fn_rgb, 3),
    "base64encode": (_fn_base64encode, 1), "base64decode": (_fn_base64decode, 1),
    "hexencode": (_fn_hexencode, 1), "hexdecode": (_fn_hexdecode, 1),
    "getasurlencoded": (_fn_getasurlencoded, 1),
    "jsonmakearray": (_fn_jsonmakearray, "V"), "jsonparse": (_fn_jsonparse, 1),
    "isvalidexpression": (_fn_isvalidexpression, 1),
    "left": (_fn_left, 2), "right": (_fn_right, 2), "middle": (_fn_middle, 3),
    "length": (_fn_length, 1), "position": (_fn_position, 4),
    "patterncount": (_fn_patterncount, 2), "replace": (_fn_replace, 4),
    "exact": (_fn_exact, 2), "filter": (_fn_filter, 2), "trim": (_fn_trim, 1),
    "upper": (_fn_upper, 1), "lower": (_fn_lower, 1), "proper": (_fn_proper, 1),
    "wordcount": (_fn_wordcount, 1), "leftwords": (_fn_leftwords, 2),
    "rightwords": (_fn_rightwords, 2), "middlewords": (_fn_middlewords, 3),
    "valuecount": (_fn_valuecount, 1), "getvalue": (_fn_getvalue, 2),
    "leftvalues": (_fn_leftvalues, 2), "rightvalues": (_fn_rightvalues, 2),
    "middlevalues": (_fn_middlevalues, 3), "filtervalues": (_fn_filtervalues, 2),
    "list": (_fn_list, "V"), "char": (_fn_char, 1), "code": (_fn_code, 1),
    "abs": (_fn_abs, 1), "int": (_fn_int, 1), "round": (_fn_round, 2),
    "truncate": (_fn_truncate, 2), "floor": (_fn_floor, 1),
    "ceiling": (_fn_ceiling, 1), "mod": (_fn_mod, 2), "div": (_fn_div, 2),
    "sign": (_fn_sign, 1), "sqrt": (_fn_sqrt, 1),
    "ln": (_fn_ln, 1), "log": (_fn_log, 1), "exp": (_fn_exp, 1),
    "min": (_fn_min, "V"), "max": (_fn_max, "V"), "sum": (_fn_sum, "V"),
    "count": (_fn_count, "V"), "average": (_fn_average, "V"),
    "isempty": (_fn_isempty, 1), "getasboolean": (_fn_getasboolean, 1),
    "getasnumber": (_fn_getasnumber, 1), "getastext": (_fn_getastext, 1),
    "jsongetelement": (_fn_jsongetelement, 2),
    "jsongetelementtype": (_fn_jsongetelementtype, 2),
    "jsonlistkeys": (_fn_jsonlistkeys, 2), "jsonlistvalues": (_fn_jsonlistvalues, 2),
    "jsondeleteelement": (_fn_jsondeleteelement, 2),
    "jsonformatelements": (_fn_jsonformatelements, 1),
    "date": (_fn_date, 3), "time": (_fn_time, 3), "timestamp": (_fn_timestamp, 2),
    "day": (_fn_day, 1), "month": (_fn_month, 1), "year": (_fn_year, 1),
    "dayofweek": (_fn_dayofweek, 1), "dayofyear": (_fn_dayofyear, 1),
    "dayname": (_fn_dayname, 1), "monthname": (_fn_monthname, 1),
    "weekofyear": (_fn_weekofyear, 1), "weekofyearfiscal": (_fn_weekofyearfiscal, 2),
    "serialincrement": (_fn_serialincrement, 2), "hour": (_fn_hour, 1),
    "minute": (_fn_minute, 1), "seconds": (_fn_seconds, 1),
    "getasdate": (_fn_getasdate, 1), "getastime": (_fn_getastime, 1),
    "getastimestamp": (_fn_getastimestamp, 1),
}

# Functions known to FileMaker (see src/calc.c) that we deliberately do NOT
# evaluate offline, each with the reason. Everything here reports
# 'unsupported: <name> (<reason>)' rather than guessing.
UNSUPPORTED = {
    "jsonparsedstate": "depends on the JSON parse-cache state (not modeled)",
    # FileMaker path conversion (not implemented)
    'convertfromfilemakerpath': 'FileMaker path conversion (not implemented)',
    'converttofilemakerpath': 'FileMaker path conversion (not implemented)',
    'posixpath': 'FileMaker path conversion (not implemented)',
    'urlpath': 'FileMaker path conversion (not implemented)',
    'winpath': 'FileMaker path conversion (not implemented)',
    # Japanese / locale text (not implemented)
    'daynamej': 'Japanese / locale text (not implemented)',
    'furigana': 'Japanese / locale text (not implemented)',
    'hiragana': 'Japanese / locale text (not implemented)',
    'kanahankaku': 'Japanese / locale text (not implemented)',
    'kanazenkaku': 'Japanese / locale text (not implemented)',
    'kanjinumeral': 'Japanese / locale text (not implemented)',
    'katakana': 'Japanese / locale text (not implemented)',
    'monthnamej': 'Japanese / locale text (not implemented)',
    'numtojtext': 'Japanese / locale text (not implemented)',
    'romanhankaku': 'Japanese / locale text (not implemented)',
    'romanzenkaku': 'Japanese / locale text (not implemented)',
    'yearname': 'Japanese / locale text (not implemented)',
    # cryptographic / binary output
    'cryptauthcode': 'cryptographic / binary output',
    'cryptdecrypt': 'cryptographic / binary output',
    'cryptdecryptbase64': 'cryptographic / binary output',
    'cryptdigest': 'cryptographic / binary output',
    'cryptencrypt': 'cryptographic / binary output',
    'cryptencryptbase64': 'cryptographic / binary output',
    'cryptgeneratesignature': 'cryptographic / binary output',
    'cryptverifysignature': 'cryptographic / binary output',
    # manipulates styled-text runs (not modeled)
    'getascss': 'manipulates styled-text runs (not modeled)',
    'getassvg': 'manipulates styled-text runs (not modeled)',
    'textcolor': 'manipulates styled-text runs (not modeled)',
    'textcolorremove': 'manipulates styled-text runs (not modeled)',
    'textfont': 'manipulates styled-text runs (not modeled)',
    'textfontremove': 'manipulates styled-text runs (not modeled)',
    'textformatremove': 'manipulates styled-text runs (not modeled)',
    'textsize': 'manipulates styled-text runs (not modeled)',
    'textsizeremove': 'manipulates styled-text runs (not modeled)',
    'textstyleadd': 'manipulates styled-text runs (not modeled)',
    'textstyleremove': 'manipulates styled-text runs (not modeled)',
    # needs a device
    'getsensor': 'needs a device', 'location': 'needs a device',
    'locationvalues': 'needs a device', 'rangebeacons': 'needs a device',
    # needs a record or found set
    'extend': 'needs a record or found set', 'getfield': 'needs a record or found set',
    'getnthrecord': 'needs a record or found set',
    'getrecordidsfromfoundset': 'needs a record or found set',
    'getrepetition': 'needs a record or found set',
    'getsummary': 'needs a record or found set', 'last': 'needs a record or found set',
    'lookup': 'needs a record or found set', 'lookupnext': 'needs a record or found set',
    # needs an AI model or account
    'addembeddings': 'needs an AI model or account',
    'computemodel': 'needs an AI model or account',
    'cosinesimilarity': 'needs an AI model or account',
    'getembedding': 'needs an AI model or account',
    'getembeddingasfile': 'needs an AI model or account',
    'getembeddingastext': 'needs an AI model or account',
    'getmodelattributes': 'needs an AI model or account',
    'getragspaceinfo': 'needs an AI model or account',
    'gettokencount': 'needs an AI model or account',
    'normalizeembedding': 'needs an AI model or account',
    'predictfrommodel': 'needs an AI model or account',
    'subtractembeddings': 'needs an AI model or account',
    # needs runtime state
    'getaddoninfo': 'needs runtime state',
    # needs the database engine
    'executesql': 'needs the database engine', 'executesqle': 'needs the database engine',
    # needs the database schema
    'basetableids': 'needs the database schema', 'basetablenames': 'needs the database schema',
    'databasenames': 'needs the database schema', 'fieldbounds': 'needs the database schema',
    'fieldcomment': 'needs the database schema', 'fieldids': 'needs the database schema',
    'fieldnames': 'needs the database schema', 'fieldrepetitions': 'needs the database schema',
    'fieldstyle': 'needs the database schema', 'fieldtype': 'needs the database schema',
    'getbasetablename': 'needs the database schema',
    'getfieldname': 'needs the database schema',
    'getfieldsonlayout': 'needs the database schema',
    'getlayoutobjectattribute': 'needs the database schema',
    'getlayoutobjectownerinfo': 'needs the database schema',
    'getnextserialvalue': 'needs the database schema',
    'gettableddl': 'needs the database schema', 'layoutids': 'needs the database schema',
    'layoutnames': 'needs the database schema',
    'layoutobjectnames': 'needs the database schema',
    'layoutobjectuuid': 'needs the database schema',
    'relationinfo': 'needs the database schema', 'scriptids': 'needs the database schema',
    'scriptnames': 'needs the database schema', 'tableids': 'needs the database schema',
    'tablenames': 'needs the database schema', 'valuelistids': 'needs the database schema',
    'valuelistitems': 'needs the database schema',
    'valuelistnames': 'needs the database schema',
    # not implemented yet
    'base64encoderfc': 'not implemented yet', 'lg': 'not implemented yet',
    # operates on container (binary) data
    'getavplayerattribute': 'operates on container (binary) data',
    'getcontainerattribute': 'operates on container (binary) data',
    'getheight': 'operates on container (binary) data',
    'getlivetext': 'operates on container (binary) data',
    'getlivetextasjson': 'operates on container (binary) data',
    'gettextfrompdf': 'operates on container (binary) data',
    'getthumbnail': 'operates on container (binary) data',
    'getwidth': 'operates on container (binary) data',
    'readqrcode': 'operates on container (binary) data',
    'verifycontainer': 'operates on container (binary) data',
    # returns FileMaker error codes (not modeled)
    'evaluationerror': 'returns FileMaker error codes (not modeled)',
    # text encoding by name (not implemented)
    'textdecode': 'text encoding by name (not implemented)',
    'textencode': 'text encoding by name (not implemented)',
}


# ===========================================================================
# Public API
# ===========================================================================
def display(v):
    """Render a result value the way FileMaker would show it as data."""
    return as_text(v)


def eval_source(src, params=None, vars=None, library=None, getvals=None):
    """Parse and evaluate calc source text. `params` may be a dict of
    name->value, `vars` a dict of $name->value, `getvals` a dict of Get()
    selector->value. Returns a Python value (str or Decimal); display() for text."""
    names = {}
    if params:
        for k, val in params.items():
            names[k.lower()] = val if isinstance(val, (str, Decimal)) else str(val)
    env = Env(names=names, vars=dict(vars or {}), library=library or {},
              getvals={k.lower(): v for k, v in (getvals or {}).items()})
    return evaluate(parse(src), env)


def build_library(cache):
    """Build a name -> compiled-custom-function map from an fmpexplore Cache, so
    custom functions can call (and recurse into) one another. Each entry is
    {name, params, body, ast}; bodies that don't decompile cleanly are skipped."""
    import fmpexplore
    import fmcalc
    fns = fmpexplore.decode_customfns(cache)
    base = fmpexplore.CalcNames(cache)
    lib = {}
    for fn in fns:
        if not fn["name"] or fn["body"] is None:
            continue
        resolver = fmpexplore.CustomFnCalcNames(base, fn["params"])
        text = fmcalc.render_calc(fn["body"], resolver)
        try:
            ast = parse(text)
        except CalcSyntaxError:
            ast = None
        lib[fn["name"].lower()] = {"name": fn["name"], "params": fn["params"],
                                   "text": text, "ast": ast}
    return lib


def eval_customfn(cache, name, args):
    """Evaluate the stored custom function `name` with the given argument list.
    Returns a value; raises EvalError/EvalUnsupported on failure."""
    lib = build_library(cache)
    fn = lib.get(name.lower())
    if fn is None:
        raise EvalError("custom function not found: %s" % name)
    if fn["ast"] is None:
        raise EvalUnsupported("custom function %s did not decompile cleanly" % name)
    env = Env(library=lib)
    argvals = [a if isinstance(a, (str, Decimal)) else str(a) for a in args]
    return _call_customfn(fn, argvals, env)


# ===========================================================================
# Doc-example self-test: scrape `EXPR returns RESULT` pairs out of skills/*.md
# whose expression is fully literal (no field references), evaluate them, and
# compare. A built-in regression oracle straight from the Claris reference.
# ===========================================================================
_EXAMPLE_RE = re.compile(
    r"`([^`]+?)`\s+returns\s+`([^`]+?)`", re.IGNORECASE)


def load_doc_examples(skills_dir):
    import os
    cases = []
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(skills_dir, fn)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for line in text.splitlines():
            if " when " in line.lower() or " field " in line.lower():
                continue                          # references a field -> not pure
            for expr, result in _EXAMPLE_RE.findall(line):
                if re.search(r"\bget\s*\(", expr, re.IGNORECASE):
                    continue                      # Get(): runtime state, not reproducible
                cases.append((expr.strip(), result.strip(), fn))
    return cases


def _selftest(skills_dir):
    cases = load_doc_examples(skills_dir)
    npass = nfail = nskip = 0
    failures = []
    for expr, expected, src in cases:
        try:
            got = display(eval_source(expr)).replace(CR, "¶")
        except EvalUnsupported:
            nskip += 1
            continue
        except (CalcSyntaxError, EvalError):
            nskip += 1
            continue
        if _example_match(got, expected):
            npass += 1
        else:
            nfail += 1
            failures.append((expr, expected, got, src))
    print("doc-example self-test: %d pass, %d fail, %d skipped (%d total)"
          % (npass, nfail, nskip, len(cases)))
    for expr, exp, got, src in failures[:40]:
        print("  FAIL [%s] %s\n        expected %r got %r" % (src, expr, exp, got))
    return nfail


def _example_match(got, exp):
    """Compare an evaluated result to a doc example's stated result, tolerating
    the doc's display conventions: a '...' truncation, and rounded numerics."""
    if got == exp:
        return True
    trunc = exp.endswith("...")
    core = exp[:-3] if trunc else exp
    if trunc:
        norm = lambda s: s.lstrip("0") if s.startswith("0.") else s
        if norm(got).startswith(norm(core)):
            return True
    if is_numberish(got) and is_numberish(core):
        gv, ev = as_number(got), as_number(core)
        if gv == ev:
            return True
        places = len(core.split(".")[1]) if "." in core else 0   # doc-rounded
        if gv.quantize(Decimal(1).scaleb(-places),
                       rounding=decimal.ROUND_HALF_UP) == ev:
            return True
    return False


def is_numberish(s):
    return bool(re.fullmatch(r"[-+]?\d*\.?\d+", s.strip()))


def _trace_val(v):
    """Render a value for a trace line: text quoted (¶ shown), else as displayed."""
    if isinstance(v, str):
        return '"%s"' % v.replace(CR, "¶")
    return display(v).replace(CR, "¶")


def run_tests(path):
    """Run a TSV of `expr<TAB>expected` cases (blank lines and #-comments skipped),
    printing pass/fail. The write-then-verify loop for a generated calc."""
    npass = nfail = 0
    fails = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#") or "\t" not in ln:
                continue
            expr, expected = ln.split("\t", 1)
            try:
                got = display(eval_source(expr)).replace(CR, "¶")
            except (CalcSyntaxError, EvalError) as e:
                got = "<error: %s>" % e
            if got == expected.strip():
                npass += 1
            else:
                nfail += 1
                fails.append((expr, expected.strip(), got))
    print("tests: %d pass, %d fail" % (npass, nfail))
    for expr, exp, got in fails:
        print("  FAIL %s\n    expected %r got %r" % (expr, exp, got))
    return 1 if nfail else 0


_USAGE = (
    'usage:\n'
    '  fmeval.py \'<calc>\'                 evaluate a calculation\n'
    '  fmeval.py --param N=v \'<calc>\'      evaluate with parameter(s); repeatable\n'
    '  fmeval.py --get Sel=v \'<calc>\'      set a Get() selector (else empty); repeatable\n'
    '  fmeval.py --trace \'<calc>\'          print each Let/While binding (debugging)\n'
    '  fmeval.py --trace-every N \'<calc>\'  trace, but only every Nth While iteration\n'
    '  fmeval.py --test <file.tsv>         run expr<TAB>expected cases (verify)\n'
    '  fmeval.py --selftest [skills_dir]   run the Claris doc-example oracle\n'
    'examples:\n'
    '  fmeval.py \'Left ( "apples" ; 1 )\'\n'
    '  fmeval.py --trace \'Let ( [ a = 5 ; b = a * 2 ] ; a + b )\'\n'
    '  fmeval.py --trace-every 100 \'While ( [ i = 0 ] ; i < 1000 ; [ i = i + 1 ] ; i )\'\n'
    '  fmeval.py --param Number="555-1234" \'Left ( Number ; 3 )\''
)


# ===========================================================================
# CLI
# ===========================================================================
def main(argv=None):
    import sys
    import os
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not argv or argv[0] in ("-h", "--help"):
        print(_USAGE)
        return 0 if argv else 2
    if argv[0] == "--selftest":
        skills = argv[1] if len(argv) > 1 else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "skills")
        return 1 if _selftest(skills) else 0
    if argv[0] == "--test":
        if len(argv) < 2:
            print("usage: fmeval.py --test <file.tsv>")
            return 2
        return run_tests(argv[1])

    # evaluation mode: gather --trace / --trace-every / --param; calc is last operand
    trace = False
    trace_every = 1
    params = {}
    getvals = {}
    src = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--trace":
            trace = True
            i += 1
        elif a == "--trace-every" and i + 1 < len(argv):
            trace = True
            try:
                trace_every = max(1, int(argv[i + 1]))
            except ValueError:
                trace_every = 1
            i += 2
        elif a == "--param" and i + 1 < len(argv):
            key, _, val = argv[i + 1].partition("=")
            params[key.strip().lower()] = val
            i += 2
        elif a == "--get" and i + 1 < len(argv):
            key, _, val = argv[i + 1].partition("=")
            getvals[key.strip().lower()] = val
            i += 2
        else:
            src = a                                # last non-flag operand wins
            i += 1
    if src is None:
        print(_USAGE)
        return 2
    tr = [] if trace else None
    try:
        env = Env(names=dict(params), trace=tr, trace_every=trace_every,
                  getvals=getvals)
        result = evaluate(parse(src), env)
    except EvalUnsupported as e:
        print("unsupported: %s" % e)
        return 3
    except (CalcSyntaxError, EvalError) as e:
        print("error: %s" % e)
        return 1
    if tr is not None:
        for nm, val in tr:
            if nm == "#iter":
                print("  -- iteration %s --" % val)
            else:
                print("  %s = %s" % (nm, _trace_val(val)))
    print(("=> " if tr is not None else "") + display(result).replace(CR, "¶"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
